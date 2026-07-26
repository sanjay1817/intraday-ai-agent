"""Unit tests for `app.auto.orchestrator.AutoTradingOrchestrator`.

Uses a fake `BrokerInterface` (LTP only) and a fake `RecommendationProvider`
(controllable per-symbol signals) alongside a REAL `PaperTradingEngine` —
integration-style for the settlement pipeline (already covered on its
own in `tests/unit/paper/`), unit-style for every decision this
orchestrator itself makes.
"""

import asyncio
from datetime import UTC, datetime, time, timedelta

import pytest

from app.auto import orchestrator as orchestrator_module
from app.auto.models import AutoTradingConfig
from app.auto.orchestrator import AutoSignal, AutoTradingOrchestrator
from app.brokers.base import BrokerInterface
from app.domain.entities.broker import (
    BrokerFunds,
    BrokerProfile,
    HistoricalBar,
    Holding,
    OrderDetail,
    OrderRequest,
    OrderResponse,
    Position,
    Quote,
    TokenBundle,
)
from app.domain.enums.trading import (
    BrokerName,
    Exchange,
    HistoricalInterval,
    OrderSide,
    OrderVariety,
)
from app.instructor.recommendation import InstructorRecommendation, RecommendationAction
from app.market.dto import MarketSessionState
from app.paper.dto import PaperOrderRequest, PaperOrderType
from app.paper.engine import PaperTradingEngine
from app.strategy.models import ConfluenceResult

#: 05:30 UTC = 11:00 IST -- safely inside market hours and well before
#: `AutoTradingConfig`'s default `square_off_time` (15:20 IST), so tests
#: that pass `now=_T0` to `_run_cycle` never spuriously trigger a
#: "market_close" exit no matter what the real wall-clock time is when
#: the test suite happens to run.
_T0 = datetime(2024, 1, 1, 5, 30, tzinfo=UTC)


class _RecordingLogger:
    """A structured-log recorder that bypasses `structlog`/stdlib
    `logging` entirely.

    `structlog.testing.capture_logs()` (and even pytest's own `caplog`)
    are unreliable here specifically because `app.core.logging
    .configure_logging` — invoked for real by any test that enters a
    full app lifespan (`with TestClient(create_app()) as client:`,
    used elsewhere in this test suite and in
    `tests/integration/test_auto_trading_e2e.py`) — both replaces the
    root logger's handlers outright (breaking `caplog`, which relies on
    its own handler staying attached) and permanently caches this
    module's logger proxy against whatever config was active the first
    time it's used (`cache_logger_on_first_use=True`, breaking
    `capture_logs`'s later reconfiguration) for the rest of the process
    — a real ordering hazard confirmed by this file's own tests failing
    only when run as part of the full suite, never in isolation. A
    hand-rolled recorder patched directly onto `app.auto.orchestrator
    .logger` sidesteps all of that.
    """

    def __init__(self) -> None:
        self.entries: list[dict[str, object]] = []

    def _record(self, event: str, **kwargs: object) -> None:
        self.entries.append({"event": event, **kwargs})

    def info(self, event: str, **kwargs: object) -> None:
        self._record(event, **kwargs)

    def warning(self, event: str, **kwargs: object) -> None:
        self._record(event, **kwargs)

    def debug(self, event: str, **kwargs: object) -> None:
        self._record(event, **kwargs)

    def error(self, event: str, **kwargs: object) -> None:
        self._record(event, **kwargs)


@pytest.fixture
def orchestrator_logs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    recorder = _RecordingLogger()
    monkeypatch.setattr(orchestrator_module, "logger", recorder)
    return recorder.entries


class _FakeBroker(BrokerInterface):
    """LTP-only fake -- every other method raises if ever called."""

    def __init__(self, ltp_by_symbol: dict[str, float]) -> None:
        self.ltp_by_symbol = dict(ltp_by_symbol)

    async def login(self) -> TokenBundle:
        raise AssertionError("not used by the orchestrator")

    async def refresh_token(self) -> TokenBundle:
        raise AssertionError("not used by the orchestrator")

    async def get_profile(self) -> BrokerProfile:
        raise AssertionError("not used by the orchestrator")

    async def get_funds(self) -> BrokerFunds:
        raise AssertionError("not used by the orchestrator")

    async def get_positions(self) -> list[Position]:
        raise AssertionError("not used by the orchestrator")

    async def get_holdings(self) -> list[Holding]:
        raise AssertionError("not used by the orchestrator")

    async def get_orders(self) -> list[OrderDetail]:
        raise AssertionError("not used by the orchestrator")

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        raise AssertionError("the orchestrator must only place PAPER orders")

    async def modify_order(self, order_id: str, order: OrderRequest) -> OrderResponse:
        raise AssertionError("not used by the orchestrator")

    async def cancel_order(
        self, order_id: str, variety: OrderVariety = OrderVariety.REGULAR
    ) -> OrderResponse:
        raise AssertionError("not used by the orchestrator")

    async def ltp(self, exchange: Exchange, tradingsymbol: str) -> Quote:
        return Quote(
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            last_price=self.ltp_by_symbol[tradingsymbol],
            timestamp=_T0,
        )

    async def historical_data(self, *args: object, **kwargs: object) -> list[HistoricalBar]:
        raise AssertionError("the orchestrator uses the recommendation provider, not this")

    async def start_websocket(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("not used by the orchestrator")

    async def stop_websocket(self) -> None:
        raise AssertionError("not used by the orchestrator")

    async def close(self) -> None:
        raise AssertionError("not used by the orchestrator")


class _FakeRecommendationProvider:
    """Returns whatever `AutoSignal` is queued for a symbol, in order."""

    def __init__(self) -> None:
        self._queues: dict[str, list[AutoSignal]] = {}
        self.calls: list[str] = []

    def queue(self, symbol: str, signal: AutoSignal) -> None:
        self._queues.setdefault(symbol, []).append(signal)

    async def get_recommendation(
        self, broker: object, broker_name: object, exchange: Exchange, symbol: str, interval: object
    ) -> AutoSignal:
        self.calls.append(symbol)
        queue = self._queues.get(symbol)
        if not queue:
            raise AssertionError(f"no queued signal for {symbol}")
        return queue.pop(0)


def make_recommendation(
    *,
    symbol: str = "INFY-EQ",
    action: RecommendationAction = RecommendationAction.BUY,
    confidence: float = 80.0,
    entry: float = 1500.0,
    stop_loss: float | None = None,
    targets: list[float] | None = None,
) -> InstructorRecommendation:
    if action is RecommendationAction.HOLD:
        return InstructorRecommendation(
            symbol=symbol,
            exchange=Exchange.NSE,
            timeframe=HistoricalInterval.FIVE_MINUTE,
            action=RecommendationAction.HOLD,
            confidence=0.0,
            confirmation_count=0,
            total_strategy_count=3,
            reasoning="HOLD: no qualifying setup.",
            generated_at=_T0,
        )

    # Defaults are computed *relative to `entry`* (a fixed 4%/2% away),
    # never a hardcoded absolute price -- a hardcoded default silently
    # breaks (fails InstructorRecommendation's own directional-consistency
    # validator) for any entry price other than the one it was tuned for.
    resolved_targets = (
        targets
        if targets is not None
        else (
            [round(entry * 1.04, 2)]
            if action is RecommendationAction.BUY
            else [round(entry * 0.96, 2)]
        )
    )
    resolved_stop = (
        stop_loss
        if stop_loss is not None
        else (
            round(entry * 0.98, 2) if action is RecommendationAction.BUY else round(entry * 1.02, 2)
        )
    )
    return InstructorRecommendation(
        symbol=symbol,
        exchange=Exchange.NSE,
        timeframe=HistoricalInterval.FIVE_MINUTE,
        action=action,
        confidence=confidence,
        entry=entry,
        stop_loss=resolved_stop,
        targets=resolved_targets,
        confirmation_count=2,
        total_strategy_count=3,
        reasoning=f"{action.value}: 2 of 3 strategies agree.",
        generated_at=_T0,
    )


def make_signal(
    *,
    symbol: str = "INFY-EQ",
    action: RecommendationAction = RecommendationAction.BUY,
    confidence: float = 80.0,
    entry: float = 1500.0,
    stop_loss: float | None = None,
    targets: list[float] | None = None,
) -> AutoSignal:
    recommendation = make_recommendation(
        symbol=symbol,
        action=action,
        confidence=confidence,
        entry=entry,
        stop_loss=stop_loss,
        targets=targets,
    )
    confluence = ConfluenceResult(
        symbol=symbol,
        timeframe=HistoricalInterval.FIVE_MINUTE,
        direction=(
            "BULLISH"
            if action is RecommendationAction.BUY
            else ("BEARISH" if action is RecommendationAction.SELL else "NONE")
        ),
        agreeing_strategies=["ema_trend", "vwap_volume_breakout"],
        conflicting_strategies=["rsi_macd_reversal"],
        confirmation_count=2,
        combined_confidence=confidence,
        signals=[],
    )
    return AutoSignal(
        recommendation=recommendation, confluence=confluence, indicators_used=["EMA", "RSI"]
    )


def make_orchestrator(
    *,
    ltp_by_symbol: dict[str, float] | None = None,
    initial_capital: float = 100_000.0,
    session: MarketSessionState = MarketSessionState.OPEN,
    **config_overrides: object,
) -> tuple[AutoTradingOrchestrator, _FakeBroker, _FakeRecommendationProvider, PaperTradingEngine]:
    broker = _FakeBroker(ltp_by_symbol or {"INFY-EQ": 1500.0})
    provider = _FakeRecommendationProvider()
    engine = PaperTradingEngine(initial_capital=initial_capital)
    config_defaults: dict[str, object] = {"symbols": ["INFY-EQ"]}
    config_defaults.update(config_overrides)
    config = AutoTradingConfig(**config_defaults)
    orchestrator = AutoTradingOrchestrator(
        broker=broker,
        broker_name=BrokerName.ANGEL_ONE,
        paper_engine=engine,
        config=config,
        recommendation_provider=provider,
        session_provider=lambda: session,
    )
    return orchestrator, broker, provider, engine


# -- entries ------------------------------------------------------------------------------


async def test_buy_signal_opens_a_position() -> None:
    orchestrator, _, provider, engine = make_orchestrator()
    provider.queue("INFY-EQ", make_signal(action=RecommendationAction.BUY, confidence=80.0))

    await orchestrator._run_cycle(now=_T0)

    position = await engine.get_position("INFY-EQ", Exchange.NSE)
    assert position is not None
    assert position.quantity > 0
    status = orchestrator.status()
    assert status.open_position_count == 1
    assert status.trades_today == 1


async def test_sell_signal_opens_a_short_position() -> None:
    orchestrator, _, provider, engine = make_orchestrator()
    provider.queue(
        "INFY-EQ", make_signal(action=RecommendationAction.SELL, confidence=80.0, entry=1500.0)
    )

    await orchestrator._run_cycle(now=_T0)

    position = await engine.get_position("INFY-EQ", Exchange.NSE)
    assert position is not None
    assert position.quantity < 0


async def test_hold_signal_does_not_enter() -> None:
    orchestrator, _, provider, engine = make_orchestrator()
    provider.queue("INFY-EQ", make_signal(action=RecommendationAction.HOLD))

    await orchestrator._run_cycle(now=_T0)

    assert await engine.get_position("INFY-EQ", Exchange.NSE) is None


async def test_confidence_below_threshold_skips_entry() -> None:
    orchestrator, _, provider, engine = make_orchestrator(confidence_threshold=70.0)
    provider.queue("INFY-EQ", make_signal(action=RecommendationAction.BUY, confidence=50.0))

    await orchestrator._run_cycle(now=_T0)

    assert await engine.get_position("INFY-EQ", Exchange.NSE) is None


async def test_existing_position_prevents_duplicate_entry() -> None:
    orchestrator, _, provider, engine = make_orchestrator()
    provider.queue("INFY-EQ", make_signal(action=RecommendationAction.BUY, confidence=80.0))
    await orchestrator._run_cycle(now=_T0)
    first_position = await engine.get_position("INFY-EQ", Exchange.NSE)
    assert first_position is not None

    provider.queue("INFY-EQ", make_signal(action=RecommendationAction.BUY, confidence=90.0))
    await orchestrator._run_cycle(now=_T0)

    second_position = await engine.get_position("INFY-EQ", Exchange.NSE)
    assert second_position is not None
    assert second_position.quantity == first_position.quantity  # unchanged, no second entry


async def test_insufficient_capital_for_minimum_quantity_skips() -> None:
    orchestrator, _, provider, engine = make_orchestrator(
        ltp_by_symbol={"INFY-EQ": 1500.0}, initial_capital=1.0, capital_fraction_per_trade=1.0
    )
    provider.queue(
        "INFY-EQ", make_signal(action=RecommendationAction.BUY, confidence=80.0, entry=1500.0)
    )

    await orchestrator._run_cycle(now=_T0)

    assert await engine.get_position("INFY-EQ", Exchange.NSE) is None


async def test_one_symbol_failing_does_not_block_the_others() -> None:
    orchestrator, _, provider, engine = make_orchestrator(
        symbols=["INFY-EQ", "TCS-EQ"], ltp_by_symbol={"INFY-EQ": 1500.0, "TCS-EQ": 3500.0}
    )
    # INFY-EQ has no queued signal -> the fake provider raises for it.
    provider.queue(
        "TCS-EQ",
        make_signal(
            symbol="TCS-EQ", action=RecommendationAction.BUY, confidence=80.0, entry=3500.0
        ),
    )

    await orchestrator._run_cycle(now=_T0)

    assert await engine.get_position("TCS-EQ", Exchange.NSE) is not None


# -- risk rejections ------------------------------------------------------------------------


async def test_max_open_positions_rejects_further_entries() -> None:
    orchestrator, _, provider, engine = make_orchestrator(
        symbols=["INFY-EQ", "TCS-EQ"],
        ltp_by_symbol={"INFY-EQ": 1500.0, "TCS-EQ": 3500.0},
        max_open_positions=1,
    )
    provider.queue(
        "INFY-EQ",
        make_signal(
            symbol="INFY-EQ", action=RecommendationAction.BUY, confidence=80.0, entry=1500.0
        ),
    )
    provider.queue(
        "TCS-EQ",
        make_signal(
            symbol="TCS-EQ", action=RecommendationAction.BUY, confidence=80.0, entry=3500.0
        ),
    )

    await orchestrator._run_cycle(now=_T0)

    positions = await engine.get_positions()
    assert len(positions) == 1


async def test_max_daily_trades_rejects_further_entries() -> None:
    orchestrator, _, provider, engine = make_orchestrator(
        symbols=["INFY-EQ", "TCS-EQ"],
        ltp_by_symbol={"INFY-EQ": 1500.0, "TCS-EQ": 3500.0},
        max_daily_trades=1,
        max_open_positions=5,
    )
    provider.queue(
        "INFY-EQ",
        make_signal(
            symbol="INFY-EQ", action=RecommendationAction.BUY, confidence=80.0, entry=1500.0
        ),
    )
    provider.queue(
        "TCS-EQ",
        make_signal(
            symbol="TCS-EQ", action=RecommendationAction.BUY, confidence=80.0, entry=3500.0
        ),
    )

    await orchestrator._run_cycle(now=_T0)

    positions = await engine.get_positions()
    assert len(positions) == 1


async def test_cooldown_rejects_entry() -> None:
    orchestrator, _, provider, engine = make_orchestrator(
        cooldown_after_consecutive_losses=1, cooldown_minutes=60.0
    )
    orchestrator._risk.record_trade_closed(-500.0, now=_T0)
    assert orchestrator._risk.cooldown_until is not None

    provider.queue("INFY-EQ", make_signal(action=RecommendationAction.BUY, confidence=80.0))
    await orchestrator._run_cycle(now=_T0 + timedelta(minutes=1))

    assert await engine.get_position("INFY-EQ", Exchange.NSE) is None


async def test_structured_log_for_risk_rejection(
    orchestrator_logs: list[dict[str, object]],
) -> None:
    orchestrator, _, provider, _ = make_orchestrator(
        symbols=["INFY-EQ", "TCS-EQ"],
        ltp_by_symbol={"INFY-EQ": 1500.0, "TCS-EQ": 3500.0},
        max_open_positions=1,
    )
    provider.queue(
        "INFY-EQ",
        make_signal(
            symbol="INFY-EQ", action=RecommendationAction.BUY, confidence=80.0, entry=1500.0
        ),
    )
    await orchestrator._run_cycle(now=_T0)
    assert (await orchestrator._engine.get_position("INFY-EQ", Exchange.NSE)) is not None

    provider.queue(
        "TCS-EQ",
        make_signal(
            symbol="TCS-EQ", action=RecommendationAction.BUY, confidence=80.0, entry=3500.0
        ),
    )
    await orchestrator._run_cycle(now=_T0)

    events = [entry["event"] for entry in orchestrator_logs]
    assert "auto_signal_generated" in events
    assert "auto_risk_rejection" in events


# -- exits -----------------------------------------------------------------------------------


async def _open_long(
    orchestrator: AutoTradingOrchestrator,
    provider: _FakeRecommendationProvider,
    engine: PaperTradingEngine,
    *,
    entry: float = 1500.0,
    stop_loss: float = 1470.0,
    targets: list[float] | None = None,
) -> None:
    provider.queue(
        "INFY-EQ",
        make_signal(
            action=RecommendationAction.BUY,
            confidence=80.0,
            entry=entry,
            stop_loss=stop_loss,
            targets=targets,
        ),
    )
    await orchestrator._run_cycle(now=_T0)
    assert await engine.get_position("INFY-EQ", Exchange.NSE) is not None


async def test_stop_loss_exit_for_long_position(orchestrator_logs: list[dict[str, object]]) -> None:
    orchestrator, broker, provider, engine = make_orchestrator()
    await _open_long(
        orchestrator, provider, engine, entry=1500.0, stop_loss=1470.0, targets=[1560.0]
    )

    broker.ltp_by_symbol["INFY-EQ"] = 1465.0  # below stop-loss
    provider.queue("INFY-EQ", make_signal(action=RecommendationAction.HOLD))

    await orchestrator._run_cycle(now=_T0)

    assert await engine.get_position("INFY-EQ", Exchange.NSE) is None
    closed_events = [e for e in orchestrator_logs if e["event"] == "auto_trade_closed"]
    assert len(closed_events) == 1
    assert closed_events[0]["reason"] == "stop_loss"


async def test_target_exit_for_long_position(orchestrator_logs: list[dict[str, object]]) -> None:
    orchestrator, broker, provider, engine = make_orchestrator()
    await _open_long(
        orchestrator, provider, engine, entry=1500.0, stop_loss=1470.0, targets=[1560.0]
    )

    broker.ltp_by_symbol["INFY-EQ"] = 1565.0  # above target
    provider.queue("INFY-EQ", make_signal(action=RecommendationAction.HOLD))

    await orchestrator._run_cycle(now=_T0)

    assert await engine.get_position("INFY-EQ", Exchange.NSE) is None
    closed = [e for e in orchestrator_logs if e["event"] == "auto_trade_closed"][0]
    assert closed["reason"] == "target"
    assert closed["pnl"] > 0  # type: ignore[operator]


async def test_stop_loss_exit_for_short_position(
    orchestrator_logs: list[dict[str, object]]
) -> None:
    orchestrator, broker, provider, engine = make_orchestrator()
    provider.queue(
        "INFY-EQ",
        make_signal(
            action=RecommendationAction.SELL,
            confidence=80.0,
            entry=1500.0,
            stop_loss=1530.0,
            targets=[1440.0],
        ),
    )
    await orchestrator._run_cycle(now=_T0)
    assert (await engine.get_position("INFY-EQ", Exchange.NSE)).quantity < 0  # type: ignore[union-attr]

    broker.ltp_by_symbol["INFY-EQ"] = 1535.0  # above stop-loss for a short
    provider.queue("INFY-EQ", make_signal(action=RecommendationAction.HOLD))

    await orchestrator._run_cycle(now=_T0)

    assert await engine.get_position("INFY-EQ", Exchange.NSE) is None
    closed = [e for e in orchestrator_logs if e["event"] == "auto_trade_closed"][0]
    assert closed["reason"] == "stop_loss"


async def test_trailing_stop_exit_for_long_position(
    orchestrator_logs: list[dict[str, object]],
) -> None:
    orchestrator, broker, provider, engine = make_orchestrator()
    await _open_long(
        orchestrator, provider, engine, entry=1500.0, stop_loss=1470.0, targets=[1600.0]
    )
    # trailing_amount == |entry - stop_loss| == 30

    # Price rises well past the target's neighborhood but not to target (600),
    # so target/stop don't fire, only the trailing stop can.
    broker.ltp_by_symbol["INFY-EQ"] = 1560.0
    provider.queue("INFY-EQ", make_signal(action=RecommendationAction.HOLD))
    await orchestrator._run_cycle(now=_T0)
    assert await engine.get_position("INFY-EQ", Exchange.NSE) is not None  # still open, just marked

    # Price pulls back by more than the trailing amount (30) from the best (1560) seen.
    broker.ltp_by_symbol["INFY-EQ"] = 1525.0
    provider.queue("INFY-EQ", make_signal(action=RecommendationAction.HOLD))
    await orchestrator._run_cycle(now=_T0)

    assert await engine.get_position("INFY-EQ", Exchange.NSE) is None
    closed = [e for e in orchestrator_logs if e["event"] == "auto_trade_closed"][0]
    assert closed["reason"] == "trailing_stop"


async def test_signal_reversal_exit_for_long_position(
    orchestrator_logs: list[dict[str, object]],
) -> None:
    orchestrator, broker, provider, engine = make_orchestrator()
    await _open_long(
        orchestrator, provider, engine, entry=1500.0, stop_loss=1470.0, targets=[1600.0]
    )

    broker.ltp_by_symbol["INFY-EQ"] = 1505.0  # nowhere near stop/target
    provider.queue(
        "INFY-EQ",
        make_signal(
            action=RecommendationAction.SELL,
            confidence=85.0,
            entry=1505.0,
            stop_loss=1520.0,
            targets=[1450.0],
        ),
    )

    await orchestrator._run_cycle(now=_T0)

    assert await engine.get_position("INFY-EQ", Exchange.NSE) is None
    closed = [e for e in orchestrator_logs if e["event"] == "auto_trade_closed"][0]
    assert closed["reason"] == "signal_reversal"


async def test_low_confidence_reversal_signal_does_not_exit() -> None:
    orchestrator, broker, provider, engine = make_orchestrator(confidence_threshold=70.0)
    await _open_long(
        orchestrator, provider, engine, entry=1500.0, stop_loss=1470.0, targets=[1600.0]
    )

    broker.ltp_by_symbol["INFY-EQ"] = 1505.0
    provider.queue(
        "INFY-EQ",
        make_signal(
            action=RecommendationAction.SELL,
            confidence=50.0,
            entry=1505.0,
            stop_loss=1520.0,
            targets=[1450.0],
        ),
    )

    await orchestrator._run_cycle(now=_T0)

    assert await engine.get_position("INFY-EQ", Exchange.NSE) is not None


async def test_market_close_forces_exit_regardless_of_price(
    orchestrator_logs: list[dict[str, object]],
) -> None:
    orchestrator, broker, provider, engine = make_orchestrator(square_off_time=time(0, 0))
    await _open_long(
        orchestrator, provider, engine, entry=1500.0, stop_loss=1470.0, targets=[1600.0]
    )

    broker.ltp_by_symbol["INFY-EQ"] = 1500.0  # unchanged; only the clock matters here
    provider.queue("INFY-EQ", make_signal(action=RecommendationAction.HOLD))

    await orchestrator._run_cycle(now=_T0)

    assert await engine.get_position("INFY-EQ", Exchange.NSE) is None
    closed = [e for e in orchestrator_logs if e["event"] == "auto_trade_closed"][0]
    assert closed["reason"] == "market_close"


async def test_externally_closed_position_stops_being_tracked_without_reexiting() -> None:
    orchestrator, broker, provider, engine = make_orchestrator()
    await _open_long(
        orchestrator, provider, engine, entry=1500.0, stop_loss=1470.0, targets=[1600.0]
    )
    key = (Exchange.NSE, "INFY-EQ")
    assert key in orchestrator._positions

    # Simulate a manual close via the paper API directly (not through the orchestrator).
    from app.domain.enums.trading import OrderSide
    from app.paper.dto import PaperOrderRequest, PaperOrderType

    await engine.place_order(
        PaperOrderRequest(
            symbol="INFY-EQ",
            exchange=Exchange.NSE,
            side=OrderSide.SELL,
            order_type=PaperOrderType.MARKET,
            quantity=orchestrator._positions[key].quantity,
        ),
        current_price=1500.0,
    )
    assert await engine.get_position("INFY-EQ", Exchange.NSE) is None

    provider.queue("INFY-EQ", make_signal(action=RecommendationAction.HOLD))
    await orchestrator._run_cycle(now=_T0)

    assert key not in orchestrator._positions


# -- lifecycle ----------------------------------------------------------------------------------


async def test_status_when_never_started() -> None:
    orchestrator, _, _, _ = make_orchestrator()

    status = orchestrator.status()

    assert status.running is False
    assert status.started_at is None
    assert status.cycle_count == 0


async def test_start_marks_running_and_stop_clears_it() -> None:
    orchestrator, _, provider, _ = make_orchestrator(poll_interval_seconds=0.05)
    provider.queue("INFY-EQ", make_signal(action=RecommendationAction.HOLD))

    await orchestrator.start()
    assert orchestrator.status().running is True

    await orchestrator.stop()
    assert orchestrator.status().running is False


async def test_start_is_idempotent() -> None:
    orchestrator, _, provider, _ = make_orchestrator(poll_interval_seconds=0.05)
    provider.queue("INFY-EQ", make_signal(action=RecommendationAction.HOLD))

    await orchestrator.start()
    first_task = orchestrator._task
    await orchestrator.start()

    assert orchestrator._task is first_task
    await orchestrator.stop()


async def test_stop_is_idempotent() -> None:
    orchestrator, _, _, _ = make_orchestrator()

    await orchestrator.stop()
    await orchestrator.stop()  # must not raise

    assert orchestrator.status().running is False


# -- emergency_stop -------------------------------------------------------------------------


async def test_emergency_stop_with_no_positions_just_stops() -> None:
    orchestrator, _, provider, _ = make_orchestrator(poll_interval_seconds=0.02)
    provider.queue("INFY-EQ", make_signal(action=RecommendationAction.HOLD))
    await orchestrator.start()

    closed_orders = await orchestrator.emergency_stop()

    assert closed_orders == []
    assert orchestrator.status().running is False


async def test_emergency_stop_flattens_an_open_position() -> None:
    orchestrator, broker, provider, engine = make_orchestrator()
    await _open_long(
        orchestrator, provider, engine, entry=1500.0, stop_loss=1470.0, targets=[1600.0]
    )
    assert await engine.get_position("INFY-EQ", Exchange.NSE) is not None

    broker.ltp_by_symbol["INFY-EQ"] = 1520.0
    closed_orders = await orchestrator.emergency_stop()

    assert len(closed_orders) == 1
    assert closed_orders[0].status.value == "COMPLETE"
    assert await engine.get_position("INFY-EQ", Exchange.NSE) is None
    assert orchestrator._positions == {}
    assert orchestrator.status().running is False


async def test_emergency_stop_flattens_a_short_position() -> None:
    orchestrator, broker, provider, engine = make_orchestrator()
    provider.queue(
        "INFY-EQ",
        make_signal(
            action=RecommendationAction.SELL,
            confidence=80.0,
            entry=1500.0,
            stop_loss=1530.0,
            targets=[1440.0],
        ),
    )
    await orchestrator._run_cycle(now=_T0)
    position = await engine.get_position("INFY-EQ", Exchange.NSE)
    assert position is not None and position.quantity < 0

    closed_orders = await orchestrator.emergency_stop()

    assert len(closed_orders) == 1
    assert closed_orders[0].side.value == "BUY"  # covers the short
    assert await engine.get_position("INFY-EQ", Exchange.NSE) is None


async def test_emergency_stop_closes_positions_not_opened_by_this_orchestrator() -> None:
    """A position placed manually via the paper API (not tracked in
    `orchestrator._positions`) must still be flattened -- "emergency
    stop" means flatten everything, not just what the bot itself knows
    about.
    """

    orchestrator, broker, _, engine = make_orchestrator()
    manual_request = PaperOrderRequest(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        order_type=PaperOrderType.MARKET,
        quantity=10,
    )
    await engine.place_order(manual_request, current_price=1500.0)
    assert orchestrator._positions == {}  # not tracked by the orchestrator
    assert await engine.get_position("INFY-EQ", Exchange.NSE) is not None

    closed_orders = await orchestrator.emergency_stop()

    assert len(closed_orders) == 1
    assert await engine.get_position("INFY-EQ", Exchange.NSE) is None


async def test_emergency_stop_price_lookup_failure_is_skipped_not_fatal() -> None:
    orchestrator, broker, provider, engine = make_orchestrator()
    await _open_long(
        orchestrator, provider, engine, entry=1500.0, stop_loss=1470.0, targets=[1600.0]
    )
    del broker.ltp_by_symbol["INFY-EQ"]  # simulate a price-lookup failure

    closed_orders = await orchestrator.emergency_stop()

    assert closed_orders == []  # skipped, not raised
    assert await engine.get_position("INFY-EQ", Exchange.NSE) is not None  # left open


async def test_emergency_stop_is_logged(orchestrator_logs: list[dict[str, object]]) -> None:
    orchestrator, broker, provider, engine = make_orchestrator()
    await _open_long(
        orchestrator, provider, engine, entry=1500.0, stop_loss=1470.0, targets=[1600.0]
    )
    broker.ltp_by_symbol["INFY-EQ"] = 1520.0

    await orchestrator.emergency_stop()

    events = [entry["event"] for entry in orchestrator_logs]
    assert "auto_emergency_stop_position_closed" in events
    assert "auto_emergency_stop_completed" in events


async def test_loop_runs_cycles_while_market_open() -> None:
    orchestrator, _, provider, _ = make_orchestrator(poll_interval_seconds=0.02)
    for _ in range(20):
        provider.queue("INFY-EQ", make_signal(action=RecommendationAction.HOLD))

    await orchestrator.start()
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if orchestrator.status().cycle_count >= 2:
                break
    finally:
        await orchestrator.stop()

    assert orchestrator.status().cycle_count >= 2


async def test_loop_does_not_run_cycles_while_market_closed() -> None:
    orchestrator, _, provider, _ = make_orchestrator(
        poll_interval_seconds=0.02, session=MarketSessionState.CLOSED
    )

    await orchestrator.start()
    await asyncio.sleep(0.1)
    await orchestrator.stop()

    assert orchestrator.status().cycle_count == 0
    assert provider.calls == []


async def test_one_cycle_failure_is_recorded_but_does_not_kill_the_loop() -> None:
    orchestrator, _, provider, _ = make_orchestrator(poll_interval_seconds=0.02)
    # No queued signal at all -> the fake provider's AssertionError propagates
    # out of _run_cycle's per-symbol try/except boundary is caught there,
    # but if _run_cycle itself somehow raised, the loop must still survive.

    await orchestrator.start()
    await asyncio.sleep(0.08)
    running_after_failures = orchestrator.status().running
    await orchestrator.stop()

    assert running_after_failures is True
