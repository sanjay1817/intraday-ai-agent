"""Automatic Paper Trading: the orchestration loop.

`AutoTradingOrchestrator` is a thin conductor over already-built
pieces — it computes nothing about indicators, strategies, or order
matching itself:

- `StrategyRecommendationProvider` (the default `RecommendationProvider`)
  calls `app.market.ingestion.fetch_recent_candles`,
  `app.strategy.engine.StrategyEngine`, and
  `app.instructor.recommendation.generate_recommendation` exactly as
  `app.api.v1.routers.signals` already does.
- Every order this class places goes through
  `app.paper.engine.PaperTradingEngine.place_order` — the same engine
  `app.api.v1.routers.paper` uses, so auto-trades show up in the
  existing `/api/v1/paper/*` endpoints as ordinary paper orders/
  positions/trades. No real broker order is ever placed.
- `app.auto.risk.AutoRiskManager` gates every entry.

The loop is a plain `asyncio.Task` (`start`/`stop`/`_run_loop`), the
same reconnect-loop shape `app.brokers.ws_client
.ReconnectingWebSocketClient` already uses elsewhere in this codebase —
deliberately not a new scheduling abstraction. It only ever runs a
trading cycle while `app.market.ingestion.current_session_state()`
reports the NSE session as `OPEN`; outside that window it just sleeps
and re-checks.

Exit management (stop-loss/target/trailing-stop/signal-reversal/
market-close) is handled entirely by this orchestrator issuing a plain
MARKET order back through the same `PaperTradingEngine.place_order`
path — not by `app.paper.dto.PaperOrderRequest`'s bracket/OCO feature.
Both are valid ways to exit a paper position, but bracket orders fix a
stop/target at entry with no way to react to a signal reversal or a
trailing stop's need to ratchet over time; this class needs uniform
control over all five exit conditions in one place, so it manages every
exit itself rather than splitting the logic between two mechanisms.
"""

import contextlib
from asyncio import CancelledError, Event, Task, create_task, wait_for
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

import structlog

from app.auto.models import AutoTradingConfig, AutoTradingStatus
from app.auto.risk import AutoRiskManager
from app.brokers.base import BrokerInterface
from app.domain.enums.trading import (
    BrokerName,
    Exchange,
    HistoricalInterval,
    OrderSide,
    OrderStatus,
)
from app.indicators.engine import IndicatorEngine
from app.instructor.recommendation import (
    InstructorRecommendation,
    RecommendationAction,
    generate_recommendation,
)
from app.market.dto import MarketSessionState
from app.market.ingestion import current_session_state, fetch_recent_candles
from app.paper.dto import PaperOrderRequest, PaperOrderType
from app.paper.engine import PaperTradingEngine
from app.paper.models import PaperOrder, TradeMetadata
from app.strategy.engine import StrategyEngine
from app.strategy.models import ConfluenceResult

logger = structlog.get_logger(__name__)

_INDIA_TZ = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True, slots=True)
class AutoSignal:
    """One symbol's per-cycle recommendation, plus the strategy-level
    detail (`app.paper.models.TradeMetadata`'s fields come from here)
    that `InstructorRecommendation` alone doesn't carry.
    """

    recommendation: InstructorRecommendation
    confluence: ConfluenceResult
    indicators_used: list[str]


class RecommendationProvider(Protocol):
    """What `AutoTradingOrchestrator` needs to turn one symbol into a
    signal — structural, so tests can inject a deterministic fake
    instead of the real market-data/strategy pipeline.
    """

    async def get_recommendation(
        self,
        broker: BrokerInterface,
        broker_name: BrokerName,
        exchange: Exchange,
        symbol: str,
        interval: HistoricalInterval,
    ) -> AutoSignal: ...


class StrategyRecommendationProvider:
    """The real, default `RecommendationProvider` — candles from the
    live broker, indicators + confluence from `StrategyEngine`, and the
    final recommendation from `app.instructor.recommendation`.
    """

    async def get_recommendation(
        self,
        broker: BrokerInterface,
        broker_name: BrokerName,
        exchange: Exchange,
        symbol: str,
        interval: HistoricalInterval,
    ) -> AutoSignal:
        candles = await fetch_recent_candles(broker, broker_name, exchange, symbol, interval)
        session = current_session_state()

        engine = StrategyEngine()
        confluence = engine.analyze_symbol(
            symbol, exchange, interval, candles, session, indicator_engine=IndicatorEngine()
        )
        recommendation = generate_recommendation(confluence, exchange)
        indicators_used = sorted(
            {request.result_key for request in engine.required_indicator_requests}
        )

        return AutoSignal(
            recommendation=recommendation, confluence=confluence, indicators_used=indicators_used
        )


@dataclass(slots=True)
class _AutoPosition:
    """This orchestrator's own record of one position it opened — not
    `app.paper.models.PaperPosition` (which has no room for a stop-loss/
    target/trailing state, and is keyed purely by net exposure, not by
    who opened it).
    """

    symbol: str
    exchange: Exchange
    side: OrderSide
    quantity: int
    entry_price: float
    stop_loss: float
    targets: list[float]
    trailing_amount: float | None
    best_price: float
    entry_order_id: str
    opened_at: datetime


class AutoTradingOrchestrator:
    """Runs the automatic paper-trading loop for `config.symbols`."""

    def __init__(
        self,
        *,
        broker: BrokerInterface,
        broker_name: BrokerName,
        paper_engine: PaperTradingEngine,
        config: AutoTradingConfig,
        recommendation_provider: RecommendationProvider | None = None,
        session_provider: Callable[[], MarketSessionState] | None = None,
    ) -> None:
        self._broker = broker
        self._broker_name = broker_name
        self._engine = paper_engine
        self._config = config
        self._provider = recommendation_provider or StrategyRecommendationProvider()
        #: Defaults to the real `current_session_state` (live NSE hours);
        #: injectable for the same reason `recommendation_provider` is —
        #: a test running outside 09:15-15:30 IST on a weekday still
        #: needs to exercise the loop deterministically.
        self._session_provider = session_provider or current_session_state
        self._risk = AutoRiskManager(config)

        self._positions: dict[tuple[Exchange, str], _AutoPosition] = {}
        self._task: Task[None] | None = None
        self._stop_event = Event()
        self._started_at: datetime | None = None
        self._cycle_count = 0
        self._last_cycle_at: datetime | None = None
        self._last_error: str | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> AutoTradingStatus:
        return AutoTradingStatus(
            running=self.is_running,
            started_at=self._started_at,
            symbols=list(self._config.symbols),
            cycle_count=self._cycle_count,
            last_cycle_at=self._last_cycle_at,
            open_position_count=len(self._positions),
            trades_today=self._risk.trades_today,
            daily_realized_pnl=self._risk.daily_realized_pnl,
            consecutive_losses=self._risk.consecutive_losses,
            cooldown_until=self._risk.cooldown_until,
            last_error=self._last_error,
        )

    # -- lifecycle --------------------------------------------------------------------------

    async def start(self) -> None:
        """Start the polling loop, if not already running. Idempotent.

        Resets the risk manager's day-scoped counters (a fresh session
        starting mid-day, e.g. after a restart, shouldn't inherit
        whatever a previous run had already accumulated).
        """

        if self.is_running:
            return
        self._stop_event.clear()
        self._risk.reset()
        self._started_at = datetime.now(UTC)
        self._cycle_count = 0
        self._last_error = None
        self._task = create_task(self._run_loop())
        logger.info("auto_trading_started", symbols=self._config.symbols)

    async def stop(self) -> None:
        """Stop the polling loop and wait for it to finish cleanly. Idempotent."""

        self._stop_event.set()
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(CancelledError):
            await self._task
        self._task = None
        logger.info("auto_trading_stopped")

    async def emergency_stop(self) -> list[PaperOrder]:
        """Stop the loop and immediately flatten *every* open paper
        position at the current market price — not just ones this
        orchestrator itself opened, since a dashboard's "Emergency Stop"
        button is expected to mean "flatten everything right now", not
        "flatten only what the bot knows about". Reuses the exact same
        `PaperTradingEngine.place_order` path every other order in this
        package goes through — no new order-execution logic.

        Returns every closing order placed (whether or not it actually
        completed — a failed close is still reported, not silently
        dropped, so a caller can see what still needs attention).
        """

        await self.stop()

        closed_orders: list[PaperOrder] = []
        for position in await self._engine.get_positions():
            side = OrderSide.SELL if position.quantity > 0 else OrderSide.BUY
            quantity = abs(position.quantity)
            try:
                quote = await self._broker.ltp(position.exchange, position.symbol)
            except (
                Exception
            ) as exc:  # noqa: BLE001 - one symbol's price failure must not block the rest
                logger.error(
                    "auto_emergency_stop_price_lookup_failed",
                    symbol=position.symbol,
                    exception=repr(exc),
                )
                continue

            request = PaperOrderRequest(
                symbol=position.symbol,
                exchange=position.exchange,
                side=side,
                order_type=PaperOrderType.MARKET,
                quantity=quantity,
            )
            metadata = TradeMetadata(reasoning="AUTO EXIT (emergency_stop)")
            order = await self._engine.place_order(
                request, current_price=quote.last_price, metadata=metadata
            )
            closed_orders.append(order)

            if order.status is OrderStatus.COMPLETE:
                logger.info(
                    "auto_emergency_stop_position_closed",
                    symbol=position.symbol,
                    order_id=order.order_id,
                )
            else:
                logger.warning(
                    "auto_emergency_stop_position_close_failed",
                    symbol=position.symbol,
                    status=order.status.value,
                    status_message=order.status_message,
                )

        self._positions.clear()
        logger.info("auto_emergency_stop_completed", positions_closed=len(closed_orders))
        return closed_orders

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                session = self._session_provider()
                if session is MarketSessionState.OPEN:
                    await self._run_cycle()
                else:
                    logger.debug("auto_trading_market_closed", session=session.value)
            except CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- one bad cycle must not kill the loop
                self._last_error = repr(exc)
                logger.error("auto_trading_cycle_failed", exception=repr(exc))
            await self._sleep_or_stop(self._config.poll_interval_seconds)

    async def _sleep_or_stop(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await wait_for(self._stop_event.wait(), timeout=seconds)

    # -- one trading cycle --------------------------------------------------------------------

    async def _run_cycle(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self._cycle_count += 1
        self._last_cycle_at = now

        signals: dict[str, AutoSignal] = {}
        for symbol in self._config.symbols:
            try:
                signal = await self._provider.get_recommendation(
                    self._broker,
                    self._broker_name,
                    self._config.exchange,
                    symbol,
                    self._config.interval,
                )
            except Exception as exc:  # noqa: BLE001 -- one symbol's failure must not skip the rest
                logger.warning("auto_signal_generation_failed", symbol=symbol, exception=repr(exc))
                continue

            signals[symbol] = signal
            logger.info(
                "auto_signal_generated",
                symbol=symbol,
                action=signal.recommendation.action.value,
                confidence=signal.recommendation.confidence,
                confirmation_count=signal.confluence.confirmation_count,
                total_strategy_count=len(signal.confluence.signals),
            )

        # Exits first: a symbol square-off/stop/target/reversal this
        # cycle must not be immediately re-entered by the entry pass
        # below using the very same cycle's signal.
        exited_symbols = await self._monitor_exits(signals, now)

        for symbol, signal in signals.items():
            if symbol in exited_symbols:
                # Just exited (stop/target/trailing/reversal/square-off)
                # this same cycle -- re-entering immediately off the same
                # signal that caused (or coincided with) the exit is a
                # whipsaw risk this orchestrator deliberately avoids; the
                # next cycle's fresh signal gets a fair chance instead.
                continue
            await self._maybe_enter(symbol, signal, now)

    # -- entries --------------------------------------------------------------------------------

    async def _maybe_enter(self, symbol: str, signal: AutoSignal, now: datetime) -> None:
        recommendation = signal.recommendation
        exchange = self._config.exchange

        if recommendation.action is RecommendationAction.HOLD:
            return

        if recommendation.confidence < self._config.confidence_threshold:
            logger.info(
                "auto_trade_skipped",
                symbol=symbol,
                reason="confidence_below_threshold",
                confidence=recommendation.confidence,
                threshold=self._config.confidence_threshold,
            )
            return

        if signal.confluence.confirmation_count < self._config.min_confirmations:
            logger.info(
                "auto_trade_skipped",
                symbol=symbol,
                reason="insufficient_strategy_confirmation",
                confirmation_count=signal.confluence.confirmation_count,
                min_confirmations=self._config.min_confirmations,
            )
            return

        if await self._engine.get_position(symbol, exchange) is not None:
            logger.info("auto_trade_skipped", symbol=symbol, reason="position_already_open")
            return

        risk_result = self._risk.check_entry_allowed(
            open_position_count=len(self._positions), now=now
        )
        if not risk_result.allowed:
            logger.info("auto_risk_rejection", symbol=symbol, reason=risk_result.reason)
            return

        # `InstructorRecommendation`'s own validator already guarantees
        # `entry`/`stop_loss` are set for a BUY/SELL action, so this can
        # never actually trigger today — kept as the same kind of safety
        # net `generate_recommendation` itself keeps against a future
        # relaxation of that guarantee, not dead code by oversight.
        entry_price = recommendation.entry
        stop_loss = recommendation.stop_loss
        if entry_price is None or stop_loss is None:
            logger.info("auto_trade_skipped", symbol=symbol, reason="incomplete_trade_plan")
            return

        portfolio = await self._engine.get_portfolio(now=now)
        max_spend = portfolio.available_cash * self._config.capital_fraction_per_trade
        quantity = int(max_spend // entry_price)
        if quantity < 1:
            logger.info(
                "auto_trade_skipped", symbol=symbol, reason="insufficient_capital_for_min_quantity"
            )
            return

        side = (
            OrderSide.BUY if recommendation.action is RecommendationAction.BUY else OrderSide.SELL
        )
        request = PaperOrderRequest(
            symbol=symbol,
            exchange=exchange,
            side=side,
            order_type=PaperOrderType.MARKET,
            quantity=quantity,
        )
        metadata = TradeMetadata(
            confidence=recommendation.confidence,
            agreeing_strategies=list(signal.confluence.agreeing_strategies),
            conflicting_strategies=list(signal.confluence.conflicting_strategies),
            indicators_used=signal.indicators_used,
            reasoning=recommendation.reasoning,
        )

        order = await self._engine.place_order(
            request, current_price=entry_price, metadata=metadata, now=now
        )
        if order.status is not OrderStatus.COMPLETE:
            logger.info(
                "auto_trade_skipped",
                symbol=symbol,
                reason=f"order_not_filled:{order.status.value}",
                status_message=order.status_message,
            )
            return

        self._risk.record_trade_opened(now=now)
        fill_price = order.average_fill_price or entry_price
        self._positions[(exchange, symbol)] = _AutoPosition(
            symbol=symbol,
            exchange=exchange,
            side=side,
            quantity=quantity,
            entry_price=fill_price,
            stop_loss=stop_loss,
            targets=list(recommendation.targets),
            trailing_amount=abs(entry_price - stop_loss),
            best_price=fill_price,
            entry_order_id=order.order_id,
            opened_at=now,
        )
        logger.info(
            "auto_trade_opened",
            symbol=symbol,
            side=side.value,
            quantity=quantity,
            entry_price=fill_price,
            stop_loss=stop_loss,
            targets=recommendation.targets,
            confidence=recommendation.confidence,
            order_id=order.order_id,
        )

    # -- exits ------------------------------------------------------------------------------------

    async def _monitor_exits(self, signals: dict[str, AutoSignal], now: datetime) -> set[str]:
        """Check every tracked position for an exit condition, act on
        any that are hit, and return the set of symbols this call
        itself exited this cycle (so `_run_cycle` can skip re-entering
        them off the very same signal — see its own comment).
        """

        square_off = self._is_square_off_time(now)
        exited: set[str] = set()

        for key, position in list(self._positions.items()):
            exchange, symbol = key

            if await self._engine.get_position(symbol, exchange) is None:
                # Closed by something other than this orchestrator (a
                # manual /api/v1/paper/order call) -- stop tracking it.
                del self._positions[key]
                continue

            quote = await self._broker.ltp(exchange, symbol)
            current_price = quote.last_price

            reason = self._exit_reason(
                position, current_price, signals.get(symbol), square_off=square_off
            )
            if reason is None:
                self._positions[key] = self._ratchet_best_price(position, current_price)
                continue

            if await self._exit_position(key, position, current_price, reason, now):
                exited.add(symbol)

        return exited

    def _is_square_off_time(self, now: datetime) -> bool:
        return now.astimezone(_INDIA_TZ).time() >= self._config.square_off_time

    @staticmethod
    def _ratchet_best_price(position: _AutoPosition, current_price: float) -> _AutoPosition:
        if position.side is OrderSide.BUY:
            best_price = max(position.best_price, current_price)
        else:
            best_price = min(position.best_price, current_price)
        return replace(position, best_price=best_price)

    def _exit_reason(
        self,
        position: _AutoPosition,
        current_price: float,
        signal: AutoSignal | None,
        *,
        square_off: bool,
    ) -> str | None:
        if square_off:
            return "market_close"

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
            if is_long and position.best_price > position.entry_price:
                trailing_level = position.best_price - position.trailing_amount
                if current_price <= trailing_level:
                    return "trailing_stop"
            elif not is_long and position.best_price < position.entry_price:
                trailing_level = position.best_price + position.trailing_amount
                if current_price >= trailing_level:
                    return "trailing_stop"

        if signal is not None and signal.recommendation.action is not RecommendationAction.HOLD:
            signal_side = (
                OrderSide.BUY
                if signal.recommendation.action is RecommendationAction.BUY
                else OrderSide.SELL
            )
            if (
                signal_side is not position.side
                and signal.recommendation.confidence >= self._config.confidence_threshold
            ):
                return "signal_reversal"

        return None

    async def _exit_position(
        self,
        key: tuple[Exchange, str],
        position: _AutoPosition,
        current_price: float,
        reason: str,
        now: datetime,
    ) -> bool:
        """Returns whether the exit actually completed."""

        exchange, symbol = key
        exit_side = OrderSide.SELL if position.side is OrderSide.BUY else OrderSide.BUY
        request = PaperOrderRequest(
            symbol=symbol,
            exchange=exchange,
            side=exit_side,
            order_type=PaperOrderType.MARKET,
            quantity=position.quantity,
        )
        metadata = TradeMetadata(reasoning=f"AUTO EXIT ({reason}) at {current_price}")

        trades_before = len(await self._engine.get_trades())
        order = await self._engine.place_order(
            request, current_price=current_price, metadata=metadata, now=now
        )

        if order.status is not OrderStatus.COMPLETE:
            logger.warning(
                "auto_trade_exit_failed",
                symbol=symbol,
                reason=reason,
                status=order.status.value,
                status_message=order.status_message,
            )
            return False  # leave it tracked -- still open, still needs managing

        del self._positions[key]
        new_trades = (await self._engine.get_trades())[trades_before:]
        pnl = sum(trade.pnl for trade in new_trades)
        self._risk.record_trade_closed(pnl, now=now)

        logger.info(
            "auto_trade_closed",
            symbol=symbol,
            reason=reason,
            exit_price=current_price,
            pnl=pnl,
            order_id=order.order_id,
            entry_order_id=position.entry_order_id,
        )
        return True
