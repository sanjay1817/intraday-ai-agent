"""Candle Replay Engine: walks one instrument's historical candles in
chronological order and runs the *exact same* `StrategyEngine` +
`generate_recommendation` pipeline `app.api.v1.routers.signals` and
`app.auto.orchestrator.AutoTradingOrchestrator` already use, against a
fresh, isolated `PaperTradingEngine` — never `app.state.paper_engine`,
never a live broker order call.

**No look-ahead bias**: at candle index `i`, the strategy only ever sees
`candles[: i + 1]` — everything from `candles[i + 1:]` onward is not yet
"in the past" at that point in the replay and is never passed to
`StrategyEngine.analyze_symbol`.

**Execution timing assumption** (documented per the feature's
requirements): a signal produced from candle `i`'s close is filled at
candle `i + 1`'s open, not candle `i`'s own close. This avoids the
unrealistic "traded at the exact instant the bar closed" assumption a
same-bar fill would make, while still being a same-cadence, next-tick
fill — the same spirit as `AutoTradingOrchestrator` acting on the latest
*closed* candle's signal via the next available broker price. The one
exception is the forced end-of-session square-off on the final candle,
which has no "next" candle to fill against and is therefore closed at
that final candle's own close (mirroring `AutoTradingOrchestrator`'s own
`square_off_time` forced exit).

Exit conditions (stop-loss/target/trailing-stop/signal-reversal/
session-end) mirror `AutoTradingOrchestrator._exit_reason` exactly, but
are re-implemented here (not imported) since that orchestrator is a live
`asyncio` polling loop, not a pure step function over a candle list.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

import structlog

from app.backtest.costs import TransactionCostModel, apply_slippage, charges_for_fill
from app.backtest.dto import BacktestTradeRecord, EquityPoint, SignalLogEntry
from app.core.logging import TRADE_LOGGER_NAME
from app.domain.enums.trading import Exchange, HistoricalInterval, OrderSide, OrderStatus
from app.domain.exceptions.indicators import InsufficientDataError
from app.indicators.engine import IndicatorEngine
from app.indicators.schemas import ADXPoint, MACDPoint, SingleValuePoint, SuperTrendPoint
from app.instructor.recommendation import RecommendationAction, generate_recommendation
from app.market.dto import MarketCandle, MarketSessionState
from app.market.indicator_runtime import compute_indicators
from app.paper.dto import PaperOrderRequest, PaperOrderType
from app.paper.engine import PaperTradingEngine
from app.paper.models import TradeMetadata
from app.strategy.engine import StrategyEngine

logger = structlog.get_logger(__name__)
trade_logger = structlog.get_logger(TRADE_LOGGER_NAME)


@dataclass(slots=True)
class _ReplayPosition:
    side: OrderSide
    quantity: int
    entry_signal_price: float
    entry_fill_price: float
    entry_charges: float
    stop_loss: float
    targets: list[float]
    trailing_amount: float | None
    best_price: float
    entry_order_id: str
    entry_time: datetime
    confidence: float | None
    reasoning: str


def _extract_indicator_snapshot(
    indicators: dict[str, object],
) -> dict[str, float | None]:
    """The latest value of every computed indicator, keyed by alias —
    only ever the indicators the current strategies actually request
    (see `app.strategy.strategies`), never invented ones.
    """

    snapshot: dict[str, float | None] = {}
    for alias, result in indicators.items():
        values = getattr(result, "values", None)
        if not values:
            continue
        point = values[-1]
        if isinstance(point, SingleValuePoint):
            snapshot[alias] = point.value
        elif isinstance(point, ADXPoint):
            snapshot[alias] = point.adx
        elif isinstance(point, SuperTrendPoint):
            snapshot[alias] = point.value
        elif isinstance(point, MACDPoint):
            snapshot[alias] = point.macd
    return snapshot


def _ratchet_best_price(position: _ReplayPosition, current_price: float) -> _ReplayPosition:
    if position.side is OrderSide.BUY:
        best_price = max(position.best_price, current_price)
    else:
        best_price = min(position.best_price, current_price)
    return replace(position, best_price=best_price)


def _exit_reason(
    position: _ReplayPosition,
    current_price: float,
    signal_action: RecommendationAction,
    signal_confidence: float,
    confidence_threshold: float,
    *,
    is_final_candle: bool,
) -> str | None:
    if is_final_candle:
        return "session_end"

    is_long = position.side is OrderSide.BUY

    if is_long and current_price <= position.stop_loss:
        return "stop_loss"
    if not is_long and current_price >= position.stop_loss:
        return "stop_loss"

    if position.targets:
        target = position.targets[0]
        if is_long and current_price >= target:
            return "target"
        if not is_long and current_price <= target:
            return "target"

    if position.trailing_amount is not None:
        if is_long and position.best_price > position.entry_fill_price:
            trailing_level = position.best_price - position.trailing_amount
            if current_price <= trailing_level:
                return "trailing_stop"
        elif not is_long and position.best_price < position.entry_fill_price:
            trailing_level = position.best_price + position.trailing_amount
            if current_price >= trailing_level:
                return "trailing_stop"

    if signal_action is not RecommendationAction.HOLD:
        signal_side = OrderSide.BUY if signal_action is RecommendationAction.BUY else OrderSide.SELL
        if signal_side is not position.side and signal_confidence >= confidence_threshold:
            return "signal_reversal"

    return None


async def run_replay(
    candles: list[MarketCandle],
    *,
    symbol: str,
    exchange: Exchange,
    interval: HistoricalInterval,
    initial_capital: float,
    confidence_threshold: float,
    capital_fraction_per_trade: float,
    cost_model: TransactionCostModel,
) -> tuple[list[BacktestTradeRecord], list[SignalLogEntry], list[EquityPoint], list[str]]:
    """Replay `candles` (ascending timestamp order) against a fresh
    `PaperTradingEngine` and return the trade records, per-candle signal
    log, equity curve, and any non-fatal warnings.
    """

    warnings: list[str] = []
    if len(candles) < 2:
        warnings.append(
            "Fewer than 2 candles in the requested window — no entry could ever fill "
            "(there is no 'next candle' to execute against)."
        )

    engine = PaperTradingEngine(initial_capital=initial_capital)
    strategy_engine = StrategyEngine()
    session = MarketSessionState.OPEN

    trades: list[BacktestTradeRecord] = []
    signal_log: list[SignalLogEntry] = []
    equity_curve: list[EquityPoint] = []

    position: _ReplayPosition | None = None
    pending_entry: tuple[OrderSide, float, float, list[float], float | None, str] | None = None
    #: Peak equity so far, for drawdown reporting.
    peak_equity = initial_capital

    for i, candle in enumerate(candles):
        is_final_candle = i == len(candles) - 1

        # -- 1. execute anything scheduled from the previous candle, at THIS candle's open --
        if pending_entry is not None:
            side, signal_price, stop_loss, targets, confidence, reasoning = pending_entry
            pending_entry = None
            position = await _open_position(
                engine,
                symbol=symbol,
                exchange=exchange,
                side=side,
                signal_price=signal_price,
                fill_price=candle.open,
                stop_loss=stop_loss,
                targets=targets,
                confidence=confidence,
                reasoning=reasoning,
                cost_model=cost_model,
                initial_capital=initial_capital,
                capital_fraction_per_trade=capital_fraction_per_trade,
                now=candle.timestamp,
            )

        # -- 2. run the strategy on everything known up to and including this candle --
        # Early candles (fewer rows than the slowest indicator's warm-up
        # period, e.g. EMA(21)/ADX(14)) raise `InsufficientDataError`
        # rather than returning a usable-but-empty result -- treated as
        # an uneventful HOLD, exactly like a live poll cycle that simply
        # has too little history yet would (see `EMATrendStrategy`'s own
        # `_no_signal` fallback for the same case once indicators DO
        # compute but their warm-up values are individually `None`).
        window = candles[: i + 1]
        indicator_engine = IndicatorEngine()
        try:
            confluence = strategy_engine.analyze_symbol(
                symbol, exchange, interval, window, session, indicator_engine=indicator_engine
            )
            recommendation = generate_recommendation(confluence, exchange, now=candle.timestamp)
            indicator_snapshot = _extract_indicator_snapshot(
                compute_indicators(
                    indicator_engine, window, strategy_engine.required_indicator_requests
                )
            )
            action = recommendation.action
            confidence = recommendation.confidence
            entry_price = recommendation.entry
            stop_loss_level = recommendation.stop_loss
            targets_list = list(recommendation.targets)
            reasoning_text = recommendation.reasoning
        except InsufficientDataError:
            action = RecommendationAction.HOLD
            confidence = 0.0
            entry_price = None
            stop_loss_level = None
            targets_list = []
            reasoning_text = "Insufficient warm-up history for this session's indicators."
            indicator_snapshot = {}

        signal_log.append(
            SignalLogEntry(
                timestamp=candle.timestamp,
                action=action.value,
                confidence=confidence,
                indicators=indicator_snapshot,
                reasoning=reasoning_text,
            )
        )

        # -- 3. manage the open position: exit, or ratchet its trailing high/low --
        if position is not None:
            reason = _exit_reason(
                position,
                candle.close,
                action,
                confidence,
                confidence_threshold,
                is_final_candle=is_final_candle,
            )
            if reason == "session_end":
                trade = await _close_position(
                    engine,
                    position,
                    symbol=symbol,
                    exchange=exchange,
                    exit_signal_price=candle.close,
                    fill_price=candle.close,
                    exit_reason=reason,
                    cost_model=cost_model,
                    now=candle.timestamp,
                )
                if trade is not None:
                    trades.append(trade)
                position = None
            elif reason is not None and not is_final_candle:
                next_open = candles[i + 1].open
                trade = await _close_position(
                    engine,
                    position,
                    symbol=symbol,
                    exchange=exchange,
                    exit_signal_price=candle.close,
                    fill_price=next_open,
                    exit_reason=reason,
                    cost_model=cost_model,
                    now=candles[i + 1].timestamp,
                )
                if trade is not None:
                    trades.append(trade)
                position = None
            else:
                position = _ratchet_best_price(position, candle.close)

        # -- 4. schedule a fresh entry for the next candle's open, if flat --
        if (
            position is None
            and not is_final_candle
            and action is not RecommendationAction.HOLD
            and confidence >= confidence_threshold
            and entry_price is not None
            and stop_loss_level is not None
        ):
            side = OrderSide.BUY if action is RecommendationAction.BUY else OrderSide.SELL
            pending_entry = (
                side,
                entry_price,
                stop_loss_level,
                targets_list,
                confidence,
                reasoning_text,
            )

        # -- 5. mark equity for the curve --
        equity = await _mark_equity(engine, symbol, exchange, candle.close, now=candle.timestamp)
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


async def _mark_equity(
    engine: PaperTradingEngine, symbol: str, exchange: Exchange, price: float, *, now: datetime
) -> float:
    await engine.update_price(symbol, exchange, price, now=now)
    portfolio = await engine.get_portfolio(now=now)
    return portfolio.equity


async def _open_position(
    engine: PaperTradingEngine,
    *,
    symbol: str,
    exchange: Exchange,
    side: OrderSide,
    signal_price: float,
    fill_price: float,
    stop_loss: float,
    targets: list[float],
    confidence: float | None,
    reasoning: str,
    cost_model: TransactionCostModel,
    initial_capital: float,
    capital_fraction_per_trade: float,
    now: datetime,
) -> _ReplayPosition | None:
    execution_price = apply_slippage(fill_price, side, cost_model)

    portfolio = await engine.get_portfolio(now=now)
    max_spend = portfolio.available_cash * capital_fraction_per_trade
    quantity = int(max_spend // execution_price)
    if quantity < 1:
        return None

    request = PaperOrderRequest(
        symbol=symbol,
        exchange=exchange,
        side=side,
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
    charges = charges_for_fill(entry_fill_price, quantity, side, cost_model)

    trade_logger.info(
        "backtest_simulated_entry",
        tag="[HISTORICAL]",
        action=f"Simulated {side.value}",
        symbol=symbol,
        exchange=exchange.value,
        quantity=quantity,
        price=entry_fill_price,
        note="NO REAL ORDER SENT",
    )

    return _ReplayPosition(
        side=side,
        quantity=quantity,
        entry_signal_price=signal_price,
        entry_fill_price=entry_fill_price,
        entry_charges=charges,
        stop_loss=stop_loss,
        targets=targets,
        trailing_amount=abs(signal_price - stop_loss),
        best_price=entry_fill_price,
        entry_order_id=order.order_id,
        entry_time=now,
        confidence=confidence,
        reasoning=reasoning,
    )


async def _close_position(
    engine: PaperTradingEngine,
    position: _ReplayPosition,
    *,
    symbol: str,
    exchange: Exchange,
    exit_signal_price: float,
    fill_price: float,
    exit_reason: str,
    cost_model: TransactionCostModel,
    now: datetime,
) -> BacktestTradeRecord | None:
    exit_side = OrderSide.SELL if position.side is OrderSide.BUY else OrderSide.BUY
    execution_price = apply_slippage(fill_price, exit_side, cost_model)

    request = PaperOrderRequest(
        symbol=symbol,
        exchange=exchange,
        side=exit_side,
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

    exit_charges = charges_for_fill(exit_fill_price, position.quantity, exit_side, cost_model)
    total_charges = position.entry_charges + exit_charges
    entry_slippage = abs(position.entry_fill_price - position.entry_signal_price) * position.quantity
    exit_slippage = abs(exit_fill_price - exit_signal_price) * position.quantity

    trade_logger.info(
        "backtest_simulated_exit",
        tag="[HISTORICAL]",
        action=f"Simulated {exit_side.value}",
        symbol=symbol,
        exchange=exchange.value,
        quantity=position.quantity,
        price=exit_fill_price,
        reason=exit_reason,
        note="NO REAL ORDER SENT",
    )

    return BacktestTradeRecord(
        symbol=symbol,
        exchange=exchange,
        side=position.side,
        quantity=position.quantity,
        entry_time=position.entry_time,
        entry_signal_price=position.entry_signal_price,
        entry_fill_price=position.entry_fill_price,
        exit_time=now,
        exit_signal_price=exit_signal_price,
        exit_fill_price=exit_fill_price,
        stop_loss=position.stop_loss,
        target=position.targets[0] if position.targets else None,
        exit_reason=exit_reason,
        gross_pnl=gross_pnl,
        charges=total_charges,
        slippage_cost=entry_slippage + exit_slippage,
        net_pnl=gross_pnl - total_charges,
        confidence=position.confidence,
        strategy_signal=position.reasoning,
    )
