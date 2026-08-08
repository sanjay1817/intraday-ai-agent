"""Options historical replay: drives entries/exits from the underlying's
own strategy signal (exactly like `app.options.recommendation
.generate_option_recommendation` maps BUY/SELL to CE/PE), but prices
every fill against the *resolved option contract's own* historical
candles — never the underlying's price.

**Known, documented limitation**: `app.options.option_chain_service
.OptionChainService` only ever exposes the *current, live* option chain
(today's available strikes/expiries from the broker's scrip master) —
there is no historical chain-as-it-existed-on-that-date source anywhere
in this codebase. This module therefore resolves one contract (strike +
expiry + option type) from the *live* chain at the moment the first
qualifying signal fires, and reports that assumption in `warnings`
rather than silently presenting it as historically precise. If Angel
One has no historical candle data at all for the resolved contract (a
common, real limitation for many strikes/expiries), the run aborts for
that session with a clear warning — it never falls back to pricing the
option off the underlying's own candles.

A long option position (buying CE or PE) has no natural underlying-price
stop-loss/target the way an equity position does — `InstructorRecommendation
.stop_loss`/`targets` are expressed in the *underlying's* price terms,
not the option premium's. This module therefore exits a position on
whichever of these comes first: a percentage-of-premium stop-loss/target
(the same simplification `app.options.auto_trading.AutoOptionsOrchestrator`
already uses live), an opposite-direction signal reversal on the
underlying, or the session's end.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import structlog

from app.backtest.costs import TransactionCostModel, apply_slippage, charges_for_fill
from app.backtest.dataset_manager import HistoricalDataProvider
from app.backtest.dto import BacktestTradeRecord, EquityPoint, SignalLogEntry
from app.core.logging import TRADE_LOGGER_NAME
from app.domain.enums.trading import Exchange, HistoricalInterval, OrderSide, OrderStatus
from app.domain.exceptions.backtest import OptionHistoricalDataUnavailableError
from app.domain.exceptions.indicators import InsufficientDataError
from app.domain.exceptions.market import MarketDataError
from app.indicators.engine import IndicatorEngine
from app.instructor.recommendation import RecommendationAction, generate_recommendation
from app.market.dto import MarketCandle, MarketSessionState
from app.options.expiry_selector import ExpirySelector
from app.options.exceptions import OptionsError
from app.options.models import ExpiryMode, OptionType, StrikeMode
from app.options.option_chain_service import OptionChainService
from app.options.strike_selector import StrikeSelector
from app.paper.dto import PaperOrderRequest, PaperOrderType
from app.paper.engine import PaperTradingEngine
from app.paper.models import TradeMetadata
from app.strategy.engine import StrategyEngine

logger = structlog.get_logger(__name__)
trade_logger = structlog.get_logger(TRADE_LOGGER_NAME)

_ACTION_TO_OPTION_TYPE = {
    RecommendationAction.BUY: OptionType.CE,
    RecommendationAction.SELL: OptionType.PE,
}


@dataclass(slots=True)
class _OptionPosition:
    entry_signal_price: float
    entry_fill_price: float
    entry_charges: float
    quantity: int
    entry_order_id: str
    entry_time: datetime
    confidence: float | None
    reasoning: str


def _candle_at_or_before(candles: list[MarketCandle], timestamp: datetime) -> MarketCandle | None:
    """The most recent candle at/before `timestamp` — a plain linear
    scan is fine at this data volume (one trading session, minute bars).
    """

    match: MarketCandle | None = None
    for candle in candles:
        if candle.timestamp > timestamp:
            break
        match = candle
    return match


async def run_options_replay(
    underlying_candles: list[MarketCandle],
    *,
    underlying: str,
    underlying_exchange: Exchange,
    interval: HistoricalInterval,
    option_chain_service: OptionChainService,
    strike_mode: StrikeMode,
    expiry_mode: ExpiryMode,
    option_dataset_provider: HistoricalDataProvider,
    initial_capital: float,
    confidence_threshold: float,
    capital_fraction_per_trade: float,
    cost_model: TransactionCostModel,
    stop_loss_percent: float = 30.0,
    target_percent: float = 50.0,
) -> tuple[list[BacktestTradeRecord], list[SignalLogEntry], list[EquityPoint], list[str]]:
    warnings: list[str] = [
        "Options replay resolves the traded contract (strike/expiry) from the CURRENT "
        "live option chain at the moment of the first signal, not the chain as it "
        "actually existed on the historical date — strikes/expiries available today "
        "may differ from what was actually tradable then."
    ]

    engine = PaperTradingEngine(initial_capital=initial_capital)
    strategy_engine = StrategyEngine()
    session = MarketSessionState.OPEN

    signal_log: list[SignalLogEntry] = []
    resolved_option_type: OptionType | None = None
    resolved_tradingsymbol: str | None = None
    option_candles: list[MarketCandle] | None = None

    trades: list[BacktestTradeRecord] = []
    equity_curve: list[EquityPoint] = []
    position: _OptionPosition | None = None
    peak_equity = initial_capital

    for i, candle in enumerate(underlying_candles):
        is_final_candle = i == len(underlying_candles) - 1
        window = underlying_candles[: i + 1]

        # Early candles raise `InsufficientDataError` before enough
        # warm-up history exists -- see `app.backtest.replay_engine`'s
        # identical handling for why this is treated as an uneventful
        # HOLD rather than a fatal error.
        try:
            confluence = strategy_engine.analyze_symbol(
                underlying,
                underlying_exchange,
                interval,
                window,
                session,
                indicator_engine=IndicatorEngine(),
            )
            recommendation = generate_recommendation(
                confluence, underlying_exchange, now=candle.timestamp
            )
            action = recommendation.action
            confidence = recommendation.confidence
            reasoning_text = recommendation.reasoning
        except InsufficientDataError:
            action = RecommendationAction.HOLD
            confidence = 0.0
            reasoning_text = "Insufficient warm-up history for this session's indicators."

        signal_log.append(
            SignalLogEntry(
                timestamp=candle.timestamp,
                action=action.value,
                confidence=confidence,
                indicators={},
                reasoning=reasoning_text,
            )
        )

        qualifies = action is not RecommendationAction.HOLD and confidence >= confidence_threshold

        # -- resolve the contract once, off the first qualifying signal --
        if resolved_tradingsymbol is None and qualifies:
            option_type = _ACTION_TO_OPTION_TYPE[action]
            try:
                chain = await option_chain_service.get_option_chain(underlying)
                expiry = ExpirySelector(default_mode=expiry_mode).select(chain.expiries())
                strikes = chain.strikes_for_expiry(expiry)
                strike = StrikeSelector(default_mode=strike_mode).select(
                    strikes, candle.close, option_type
                )
                instrument = chain.instrument_for(expiry, strike, option_type)
                if instrument is None:
                    raise OptionHistoricalDataUnavailableError(
                        f"no chain instrument for {underlying} expiry={expiry} "
                        f"strike={strike} type={option_type.value}"
                    )
                resolved_option_type = option_type
                resolved_tradingsymbol = instrument.tradingsymbol

                option_candles = await option_dataset_provider.get_candles(
                    resolved_tradingsymbol,
                    Exchange.NFO,
                    interval,
                    underlying_candles[0].timestamp,
                    underlying_candles[-1].timestamp,
                )
                warnings.append(
                    f"Resolved contract: {resolved_tradingsymbol} ({option_type.value}, "
                    f"strike {strike:g}, expiry {expiry.isoformat()}) — historical data available, "
                    "trades will be priced against this contract's own candles."
                )
            except (OptionsError, MarketDataError, OptionHistoricalDataUnavailableError) as exc:
                warnings.append(
                    f"No historical data available for the resolved option contract "
                    f"({exc}) — reporting this limitation rather than pricing the option "
                    "off the underlying's price. No option trades were simulated for this session."
                )
                option_candles = None
                break

        # -- entry (only once, only if a contract with real data was resolved) --
        if (
            position is None
            and option_candles is not None
            and resolved_option_type is not None
            and qualifies
            and _ACTION_TO_OPTION_TYPE[action] is resolved_option_type
            and not is_final_candle
        ):
            option_candle = _candle_at_or_before(option_candles, candle.timestamp)
            if option_candle is not None:
                position = await _open_option_position(
                    engine,
                    resolved_tradingsymbol,  # type: ignore[arg-type]
                    signal_price=option_candle.close,
                    fill_price=option_candle.close,
                    confidence=confidence,
                    reasoning=reasoning_text,
                    cost_model=cost_model,
                    capital_fraction_per_trade=capital_fraction_per_trade,
                    now=candle.timestamp,
                )

        # -- exits: premium stop/target, opposite-direction reversal, or session end --
        elif position is not None and option_candles is not None:
            option_candle = _candle_at_or_before(option_candles, candle.timestamp)
            if option_candle is not None:
                current_premium = option_candle.close
                pnl_percent = (
                    (current_premium - position.entry_fill_price) / position.entry_fill_price * 100.0
                )
                reversal = (
                    qualifies and _ACTION_TO_OPTION_TYPE[action] is not resolved_option_type
                )
                reason = None
                if is_final_candle:
                    reason = "session_end"
                elif pnl_percent <= -stop_loss_percent:
                    reason = "premium_stop_loss"
                elif pnl_percent >= target_percent:
                    reason = "premium_target"
                elif reversal:
                    reason = "signal_reversal"

                if reason is not None:
                    trade = await _close_option_position(
                        engine,
                        position,
                        resolved_tradingsymbol,  # type: ignore[arg-type]
                        exit_signal_price=current_premium,
                        fill_price=current_premium,
                        exit_reason=reason,
                        cost_model=cost_model,
                        now=candle.timestamp,
                    )
                    if trade is not None:
                        trades.append(trade)
                    position = None

        equity = await _mark_option_equity(
            engine, resolved_tradingsymbol, option_candles, candle.timestamp, initial_capital
        )
        peak_equity = max(peak_equity, equity)
        drawdown = max(peak_equity - equity, 0.0)
        drawdown_percent = (drawdown / peak_equity * 100.0) if peak_equity > 0 else 0.0
        equity_curve.append(
            EquityPoint(
                timestamp=candle.timestamp,
                equity=equity,
                drawdown=drawdown,
                drawdown_percent=drawdown_percent,
            )
        )

    return trades, signal_log, equity_curve, warnings


async def _mark_option_equity(
    engine: PaperTradingEngine,
    tradingsymbol: str | None,
    option_candles: list[MarketCandle] | None,
    timestamp: datetime,
    initial_capital: float,
) -> float:
    if tradingsymbol is None or option_candles is None:
        return initial_capital
    option_candle = _candle_at_or_before(option_candles, timestamp)
    if option_candle is not None:
        await engine.update_price(tradingsymbol, Exchange.NFO, option_candle.close, now=timestamp)
    portfolio = await engine.get_portfolio(now=timestamp)
    return portfolio.equity


async def _open_option_position(
    engine: PaperTradingEngine,
    tradingsymbol: str,
    *,
    signal_price: float,
    fill_price: float,
    confidence: float | None,
    reasoning: str,
    cost_model: TransactionCostModel,
    capital_fraction_per_trade: float,
    now: datetime,
) -> _OptionPosition | None:
    execution_price = apply_slippage(fill_price, OrderSide.BUY, cost_model)

    portfolio = await engine.get_portfolio(now=now)
    max_spend = portfolio.available_cash * capital_fraction_per_trade
    quantity = int(max_spend // execution_price)
    if quantity < 1:
        return None

    request = PaperOrderRequest(
        symbol=tradingsymbol,
        exchange=Exchange.NFO,
        side=OrderSide.BUY,
        order_type=PaperOrderType.MARKET,
        quantity=quantity,
    )
    metadata = TradeMetadata(confidence=confidence, reasoning=reasoning)
    order = await engine.place_order(
        request, current_price=execution_price, metadata=metadata, now=now
    )
    if order.status is not OrderStatus.COMPLETE:
        return None

    entry_fill_price = order.average_fill_price or execution_price
    charges = charges_for_fill(entry_fill_price, quantity, OrderSide.BUY, cost_model)

    trade_logger.info(
        "backtest_simulated_entry",
        tag="[HISTORICAL]",
        action="Simulated BUY (option)",
        symbol=tradingsymbol,
        exchange=Exchange.NFO.value,
        quantity=quantity,
        price=entry_fill_price,
        note="NO REAL ORDER SENT",
    )

    return _OptionPosition(
        entry_signal_price=signal_price,
        entry_fill_price=entry_fill_price,
        entry_charges=charges,
        quantity=quantity,
        entry_order_id=order.order_id,
        entry_time=now,
        confidence=confidence,
        reasoning=reasoning,
    )


async def _close_option_position(
    engine: PaperTradingEngine,
    position: _OptionPosition,
    tradingsymbol: str,
    *,
    exit_signal_price: float,
    fill_price: float,
    exit_reason: str,
    cost_model: TransactionCostModel,
    now: datetime,
) -> BacktestTradeRecord | None:
    execution_price = apply_slippage(fill_price, OrderSide.SELL, cost_model)

    request = PaperOrderRequest(
        symbol=tradingsymbol,
        exchange=Exchange.NFO,
        side=OrderSide.SELL,
        order_type=PaperOrderType.MARKET,
        quantity=position.quantity,
    )
    metadata = TradeMetadata(reasoning=f"HISTORICAL EXIT ({exit_reason})")

    trades_before = len(await engine.get_trades())
    order = await engine.place_order(
        request, current_price=execution_price, metadata=metadata, now=now
    )
    if order.status is not OrderStatus.COMPLETE:
        return None

    closed = (await engine.get_trades())[trades_before:]
    gross_pnl = sum(trade.pnl for trade in closed)
    exit_fill_price = order.average_fill_price or execution_price

    exit_charges = charges_for_fill(exit_fill_price, position.quantity, OrderSide.SELL, cost_model)
    total_charges = position.entry_charges + exit_charges
    entry_slippage = abs(position.entry_fill_price - position.entry_signal_price) * position.quantity
    exit_slippage = abs(exit_fill_price - exit_signal_price) * position.quantity

    trade_logger.info(
        "backtest_simulated_exit",
        tag="[HISTORICAL]",
        action="Simulated SELL (option)",
        symbol=tradingsymbol,
        exchange=Exchange.NFO.value,
        quantity=position.quantity,
        price=exit_fill_price,
        reason=exit_reason,
        note="NO REAL ORDER SENT",
    )

    return BacktestTradeRecord(
        symbol=tradingsymbol,
        exchange=Exchange.NFO,
        side=OrderSide.BUY,
        quantity=position.quantity,
        entry_time=position.entry_time,
        entry_signal_price=position.entry_signal_price,
        entry_fill_price=position.entry_fill_price,
        exit_time=now,
        exit_signal_price=exit_signal_price,
        exit_fill_price=exit_fill_price,
        stop_loss=None,
        target=None,
        exit_reason=exit_reason,
        gross_pnl=gross_pnl,
        charges=total_charges,
        slippage_cost=entry_slippage + exit_slippage,
        net_pnl=gross_pnl - total_charges,
        confidence=position.confidence,
        strategy_signal=position.reasoning,
    )
