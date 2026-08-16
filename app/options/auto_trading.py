"""Phase 5: fully automated PAPER option trading — a scheduler that scans
configured underlyings, enters/monitors/exits simulated option positions
on a timer, with no real broker order ever placed.

`AutoOptionsOrchestrator` is a thin conductor, exactly like
`app.auto.orchestrator.AutoTradingOrchestrator` is for equity: it computes
nothing about strikes, expiries, premiums, or risk itself. Every piece of
actual work is delegated to already-built Phase 2/3 functions:

- `app.options.recommendation.generate_option_recommendation` turns one
  underlying into a BULLISH/BEARISH/NO_TRADE `OptionRecommendation`.
- `app.options.paper_trading.enter_option_position`/`exit_option_position`
  place/close the simulated order through `app.paper.engine
  .PaperTradingEngine.place_order` — the exact same paper-only path every
  other options endpoint already uses. No real broker order is ever
  placed; this module never calls `BrokerInterface.place_order`,
  `modify_order`, or `cancel_order`.
- `app.options.risk.OptionRiskManager` gates every entry, invoked
  internally by `enter_option_position` — this module never calls
  `check_order_allowed` a second time itself.

Why a new, separate class instead of teaching `AutoTradingOrchestrator`
about a second "mode": that class is deeply, deliberately equity-specific
— its `_AutoPosition`/`_exit_reason` machinery is built around
`InstructorRecommendation`'s `entry`/`stop_loss`/`targets` fields, its
`AutoRiskManager` day-rollover counters, and price-based exits (trailing
stop, signal reversal) that have no options-domain equivalent here.
`app.options.risk.OptionRiskManager`'s own docstring already establishes
the precedent this class follows: mirror the *shape* of an existing
equity-side class (day-rollover pattern there; loop/task/status shape
here) as a fully independent class with no import from `app.auto`, rather
than subclassing or branching inside it. Forcing options into
`AutoTradingOrchestrator` would produce exactly the messy dual-domain
class both of those precedents were written to avoid.

Stop-loss/target are a deliberate simplification: `OptionRecommendation`
carries no price targets of its own (only `confidence`/`reasoning` — see
`app.options.schemas`), unlike `InstructorRecommendation`. This
orchestrator computes both itself as a fixed percentage of the *actual
fill premium* (`Settings.auto_option_stop_loss_percent`/
`auto_option_target_percent`): `stop_loss_premium = entry_premium * (1 -
stop_loss_percent / 100)`, `target_premium = entry_premium * (1 +
target_percent / 100)`. There is no Greeks/IV modeling anywhere in this
phase — a documented limitation, not an oversight (see
`docs/OPTIONS_PHASE5.md`).

Exits are managed explicitly by this orchestrator every cycle (comparing
a freshly fetched premium against the tracked stop/target/exit-time),
via a plain `exit_option_position(..., reason=...)` call — never via
`enter_option_position`'s own `stop_loss_premium`/`target_premium`
bracket-order parameters (always passed as `None` from here). The reason
is the same one `app.auto.orchestrator`'s module docstring gives for
managing equity exits itself rather than using paper bracket orders, plus
an options-specific gap on top of it: a bracket order only ever fills
when something feeds fresh NFO prices into
`PaperTradingEngine.update_price`, and only `app.paper.broker.PaperBroker
.ltp()` self-feeds the engine that way — a raw (non-`PaperBroker`)
`BrokerInterface` such as a real `AngelOneAdapter` never does. Explicit
per-cycle monitoring (calling `broker.ltp()` directly and comparing
against the tracked levels) sidesteps that gap entirely, and gives one
place to also handle the market-close exit uniformly.

"One active position per underlying": positions are tracked in
`self._positions`, keyed by the underlying string (not by tradingsymbol)
— this also naturally prevents ever holding two different strikes/
expiries for the same underlying at once.
"""

from __future__ import annotations

import contextlib
from asyncio import CancelledError, Event, Task, create_task, wait_for
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import structlog
from pydantic import BaseModel, ConfigDict, Field

from app.brokers.base import BrokerInterface
from app.domain.enums.trading import BrokerName, Exchange, HistoricalInterval, OrderStatus
from app.market.dto import MarketSessionState
from app.market.ingestion import current_session_state
from app.options.exceptions import OptionRiskLimitExceededError
from app.options.models import ExpiryMode, StrikeMode
from app.options.option_chain_service import OptionChainService
from app.options.paper_trading import enter_option_position, exit_option_position
from app.options.recommendation import generate_option_recommendation
from app.options.risk import OptionRiskManager
from app.options.schemas import OptionExitReason, OptionSignal
from app.options.underlying_resolver import UnderlyingResolver
from app.paper.engine import PaperTradingEngine

logger = structlog.get_logger(__name__)

#: Matches `app.auto.orchestrator`'s own IST convention for "is it time
#: to square off" — day-scoped wall-clock comparisons happen in IST, not
#: UTC, since that's what "market close" means for an NSE trading desk.
_INDIA_TZ = ZoneInfo("Asia/Kolkata")


@dataclass(slots=True)
class _AutoOptionPosition:
    """This orchestrator's own record of one position it opened — not
    `app.paper.models.PaperPosition` (which has no room for a stop-loss/
    target/underlying-linkage state), analogous to
    `app.auto.orchestrator._AutoPosition`.
    """

    underlying: str
    tradingsymbol: str
    quantity: int
    entry_premium: float
    stop_loss_premium: float
    target_premium: float
    entry_order_id: str
    opened_at: datetime


@dataclass(frozen=True, slots=True)
class AutoOptionsConfig:
    """Every knob `AutoOptionsOrchestrator` runs against. Constructed
    once from `app.config.settings.Settings` at application startup (see
    `app.main`'s lifespan) — a plain frozen dataclass, not a pydantic
    `BaseModel`, since (unlike `app.auto.models.AutoTradingConfig`) it is
    never itself a FastAPI request/response body and has no validation
    need beyond what `Settings` already performs on its own fields.
    """

    underlyings: list[str]
    timeframe: HistoricalInterval
    strike_mode: StrikeMode
    expiry_mode: ExpiryMode
    confidence_threshold: float
    #: The options equivalent of `app.auto.models.AutoTradingConfig
    #: .min_confirmations` — minimum `OptionRecommendation
    #: .confirmation_count` required before a new entry is placed. See
    #: `app.config.settings.Settings.auto_option_min_confirmations`.
    min_confirmations: int
    max_open_positions: int
    lots_per_trade: int
    scan_interval_seconds: float
    exit_time: time
    stop_loss_percent: float
    target_percent: float


class AutoOptionsStatus(BaseModel):
    """`GET /api/v1/options/auto/status`'s response — a point-in-time
    snapshot of `AutoOptionsOrchestrator`'s own state, analogous to
    `app.auto.models.AutoTradingStatus`.
    """

    model_config = ConfigDict(frozen=True)

    running: bool
    started_at: datetime | None = None
    underlyings: list[str] = Field(default_factory=list)
    cycle_count: int = Field(default=0, ge=0)
    last_cycle_at: datetime | None = None
    last_scan_at: datetime | None = None
    #: Computed as `last_scan_at + scan_interval_seconds` while running;
    #: `None` when stopped (there is no next scan to report).
    next_scan_at: datetime | None = None
    last_action: str = "idle"
    open_position_count: int = Field(default=0, ge=0)
    #: This orchestrator's own simple counter, incremented on every
    #: entry it places — not read from `OptionRiskManager` (which tracks
    #: only `daily_realized_pnl`, no trade count of its own).
    trades_today: int = Field(default=0, ge=0)
    daily_realized_pnl: float = 0.0
    last_error: str | None = None


class AutoOptionsOrchestrator:
    """Runs the automatic PAPER option-trading loop for
    `config.underlyings`.
    """

    def __init__(
        self,
        *,
        broker: BrokerInterface,
        broker_name: BrokerName,
        paper_engine: PaperTradingEngine,
        option_chain_service: OptionChainService,
        underlying_resolver: UnderlyingResolver,
        risk_manager: OptionRiskManager,
        config: AutoOptionsConfig,
        default_lot_size: int,
        max_lots_per_order: int,
        session_provider: Callable[[], MarketSessionState] | None = None,
    ) -> None:
        self._broker = broker
        self._broker_name = broker_name
        self._engine = paper_engine
        self._option_chain_service = option_chain_service
        self._underlying_resolver = underlying_resolver
        self._risk_manager = risk_manager
        self._config = config
        self._default_lot_size = default_lot_size
        self._max_lots_per_order = max_lots_per_order
        #: Defaults to the real `current_session_state` (live NSE hours);
        #: injectable for the same reason `app.auto.orchestrator
        #: .AutoTradingOrchestrator`'s own `session_provider` is.
        self._session_provider = session_provider or current_session_state

        self._positions: dict[str, _AutoOptionPosition] = {}
        self._task: Task[None] | None = None
        self._stop_event = Event()
        self._started_at: datetime | None = None
        self._cycle_count = 0
        self._trades_today = 0
        self._last_cycle_at: datetime | None = None
        self._last_scan_at: datetime | None = None
        self._last_action = "idle"
        self._last_error: str | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> AutoOptionsStatus:
        next_scan_at = (
            self._last_scan_at + timedelta(seconds=self._config.scan_interval_seconds)
            if self.is_running and self._last_scan_at is not None
            else None
        )
        return AutoOptionsStatus(
            running=self.is_running,
            started_at=self._started_at,
            underlyings=list(self._config.underlyings),
            cycle_count=self._cycle_count,
            last_cycle_at=self._last_cycle_at,
            last_scan_at=self._last_scan_at,
            next_scan_at=next_scan_at,
            last_action=self._last_action,
            open_position_count=len(self._positions),
            trades_today=self._trades_today,
            daily_realized_pnl=self._risk_manager.daily_realized_pnl,
            last_error=self._last_error,
        )

    # -- lifecycle --------------------------------------------------------------------------

    async def start(self) -> None:
        """Start the polling loop, if not already running. Idempotent.

        Resets this orchestrator's own cycle/trade counters, but does
        NOT reset `self._risk_manager` — it's a shared, injected object
        (constructed once in `app.main`'s lifespan alongside
        `option_chain_service`), not one this orchestrator owns; other
        code may also depend on its counters, and Phase 3 never gave
        callers a reason to reset it on every orchestrator start.
        """

        if self.is_running:
            return
        self._stop_event.clear()
        self._started_at = datetime.now(UTC)
        self._cycle_count = 0
        self._trades_today = 0
        self._last_error = None
        self._last_action = "idle"
        self._task = create_task(self._run_loop())
        logger.info("auto_options_trading_started", underlyings=self._config.underlyings)

    async def stop(self) -> None:
        """Stop the polling loop and wait for it to finish cleanly. Idempotent."""

        self._stop_event.set()
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(CancelledError):
            await self._task
        self._task = None
        logger.info("auto_options_trading_stopped")

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                session = self._session_provider()
                if session is MarketSessionState.OPEN:
                    await self._run_cycle()
                else:
                    self._last_action = "market_closed"
                    logger.debug("auto_options_market_closed", session=session.value)
            except CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- one bad cycle must not kill the loop
                self._last_error = repr(exc)
                logger.error("auto_options_cycle_failed", exception=repr(exc))
            await self._sleep_or_stop(self._config.scan_interval_seconds)

    async def _sleep_or_stop(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await wait_for(self._stop_event.wait(), timeout=seconds)

    # -- one trading cycle --------------------------------------------------------------------

    async def _run_cycle(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self._cycle_count += 1
        self._last_cycle_at = now
        self._last_scan_at = now
        self._last_action = "scanning"

        # Exits first: an underlying square-off/stop/target this cycle
        # must not be immediately re-entered by the entry pass below —
        # the next cycle's fresh recommendation gets a fair chance
        # instead (mirrors `app.auto.orchestrator._run_cycle`'s own
        # ordering and same-cycle re-entry guard).
        exited_underlyings = await self._monitor_exits(now)

        for underlying in self._config.underlyings:
            if underlying in exited_underlyings:
                continue
            await self._maybe_enter(underlying, now)

    # -- entries --------------------------------------------------------------------------------

    async def _maybe_enter(self, underlying: str, now: datetime) -> None:
        if underlying in self._positions:
            return

        if len(self._positions) >= self._config.max_open_positions:
            logger.info(
                "auto_option_trade_skipped",
                underlying=underlying,
                reason="max_open_option_positions_reached",
            )
            return

        try:
            recommendation = await generate_option_recommendation(
                broker=self._broker,
                broker_name=self._broker_name,
                option_chain_service=self._option_chain_service,
                underlying_resolver=self._underlying_resolver,
                underlying=underlying,
                timeframe=self._config.timeframe,
                strike_mode=self._config.strike_mode,
                expiry_mode=self._config.expiry_mode,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 -- one underlying's failure must not skip the rest
            logger.warning(
                "auto_option_signal_generation_failed", underlying=underlying, exception=repr(exc)
            )
            return

        logger.info(
            "auto_option_signal_generated",
            underlying=underlying,
            signal=recommendation.signal.value,
            confidence=recommendation.confidence,
        )

        if recommendation.signal is OptionSignal.NO_TRADE:
            logger.info("auto_option_trade_skipped", underlying=underlying, reason="no_trade")
            return

        if recommendation.confidence < self._config.confidence_threshold:
            logger.info(
                "auto_option_trade_skipped",
                underlying=underlying,
                reason="confidence_below_threshold",
                confidence=recommendation.confidence,
                threshold=self._config.confidence_threshold,
            )
            return

        if recommendation.confirmation_count < self._config.min_confirmations:
            logger.info(
                "auto_option_trade_skipped",
                underlying=underlying,
                reason="insufficient_strategy_confirmation",
                confirmation_count=recommendation.confirmation_count,
                min_confirmations=self._config.min_confirmations,
            )
            return

        try:
            order = await enter_option_position(
                broker=self._broker,
                engine=self._engine,
                option_chain_service=self._option_chain_service,
                risk_manager=self._risk_manager,
                recommendation=recommendation,
                lots=self._config.lots_per_trade,
                default_lot_size=self._default_lot_size,
                max_lots_per_order=self._max_lots_per_order,
                stop_loss_premium=None,
                target_premium=None,
                now=now,
            )
        except OptionRiskLimitExceededError as exc:
            logger.info("auto_option_risk_rejection", underlying=underlying, reason=exc.reason)
            return
        except Exception as exc:  # noqa: BLE001 -- any other options/broker failure must not skip the rest
            logger.warning("auto_option_entry_failed", underlying=underlying, exception=repr(exc))
            return

        if order.status is not OrderStatus.COMPLETE:
            logger.info(
                "auto_option_trade_skipped",
                underlying=underlying,
                reason="order_not_filled",
                status=order.status.value,
            )
            return

        # Guaranteed set on a COMPLETE order (see `PaperOrder`'s own
        # validator) -- asserted so mypy narrows it, not defensively
        # re-checked as a business rule.
        assert order.average_fill_price is not None
        fill_price = order.average_fill_price
        stop_loss_premium = fill_price * (1 - self._config.stop_loss_percent / 100)
        target_premium = fill_price * (1 + self._config.target_percent / 100)

        self._positions[underlying] = _AutoOptionPosition(
            underlying=underlying,
            tradingsymbol=order.symbol,
            quantity=order.quantity,
            entry_premium=fill_price,
            stop_loss_premium=stop_loss_premium,
            target_premium=target_premium,
            entry_order_id=order.order_id,
            opened_at=now,
        )
        self._trades_today += 1
        self._last_action = f"trade_entered:{underlying}"
        logger.info(
            "auto_option_trade_opened",
            underlying=underlying,
            tradingsymbol=order.symbol,
            quantity=order.quantity,
            entry_premium=fill_price,
            stop_loss_premium=stop_loss_premium,
            target_premium=target_premium,
            confidence=recommendation.confidence,
            order_id=order.order_id,
        )

    # -- exits ------------------------------------------------------------------------------------

    async def _monitor_exits(self, now: datetime) -> set[str]:
        """Check every tracked position for an exit condition, act on
        any that are hit, and return the set of underlyings this call
        itself exited this cycle (so `_run_cycle` can skip re-entering
        them off the very same cycle — see its own comment).
        """

        square_off = now.astimezone(_INDIA_TZ).time() >= self._config.exit_time
        exited: set[str] = set()

        for underlying, position in list(self._positions.items()):
            if await self._engine.get_position(position.tradingsymbol, Exchange.NFO) is None:
                # Closed by something other than this orchestrator (a
                # manual /api/v1/options/paper/exit call) -- stop
                # tracking it, without attempting a second exit.
                del self._positions[underlying]
                continue

            try:
                quote = await self._broker.ltp(Exchange.NFO, position.tradingsymbol)
            except Exception as exc:  # noqa: BLE001 -- one symbol's price failure must not block the rest
                logger.warning(
                    "auto_option_premium_lookup_failed",
                    underlying=underlying,
                    tradingsymbol=position.tradingsymbol,
                    exception=repr(exc),
                )
                continue
            current_premium = quote.last_price

            # `OptionExitReason` has no dedicated `MARKET_CLOSE` member
            # (only `MANUAL`/`TARGET`/`STOP_LOSS` -- see
            # `app.options.schemas`, which this phase must not modify):
            # a square-off exit is reported to `exit_option_position` as
            # `MANUAL` (an actively-driven, non-bracket close, which is
            # exactly what `MANUAL`'s own docstring describes) and
            # distinguished from an operator-initiated manual exit only
            # by this module's own `auto_option_market_close_exit` log
            # event below and `last_action`'s `market_close` suffix.
            reason: OptionExitReason | None = None
            if square_off:
                reason = OptionExitReason.MANUAL
            elif current_premium <= position.stop_loss_premium:
                reason = OptionExitReason.STOP_LOSS
            elif current_premium >= position.target_premium:
                reason = OptionExitReason.TARGET

            if reason is None:
                continue

            try:
                await exit_option_position(
                    broker=self._broker,
                    engine=self._engine,
                    risk_manager=self._risk_manager,
                    tradingsymbol=position.tradingsymbol,
                    reason=reason,
                    now=now,
                )
            except Exception as exc:  # noqa: BLE001 -- a failed exit attempt must not crash the cycle
                logger.warning(
                    "auto_option_exit_failed",
                    underlying=underlying,
                    tradingsymbol=position.tradingsymbol,
                    reason=reason.value,
                    exception=repr(exc),
                )
                continue

            del self._positions[underlying]
            exited.add(underlying)
            self._last_action = f"trade_exited:{underlying}:{reason.value.lower()}"

            log_event = {
                OptionExitReason.TARGET: "auto_option_target_exit",
                OptionExitReason.STOP_LOSS: "auto_option_stop_loss_exit",
                OptionExitReason.MANUAL: "auto_option_market_close_exit",
            }[reason]
            logger.info(
                log_event,
                underlying=underlying,
                tradingsymbol=position.tradingsymbol,
                exit_premium=current_premium,
                entry_premium=position.entry_premium,
            )

        return exited
