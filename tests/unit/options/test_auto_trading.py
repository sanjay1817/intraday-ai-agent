"""Unit tests for `app.options.auto_trading.AutoOptionsOrchestrator`.

Integration-style, mirroring `tests/unit/options/test_recommendation.py`'s
and `tests/unit/options/test_paper_trading.py`'s conventions: a REAL
`PaperTradingEngine`/`OptionChainService`/`OptionRiskManager`, a
hand-written `BrokerInterface` fake (candles for the strategy pipeline,
LTP for premiums), and deterministic `now` -- rather than faking
`generate_option_recommendation`/`enter_option_position` themselves, since
`AutoOptionsOrchestrator` calls those real module-level functions directly
(no injectable `RecommendationProvider` seam exists for options, unlike
`app.auto.orchestrator`'s equity-specific one).
"""

import asyncio
from datetime import UTC, date, datetime, time, timedelta

import pytest

from app.domain.entities.broker import HistoricalBar, Quote
from app.domain.enums.trading import BrokerName, Exchange, HistoricalInterval, OrderSide
from app.market.dto import MarketSessionState
from app.options import auto_trading as auto_trading_module
from app.options.auto_trading import AutoOptionsConfig, AutoOptionsOrchestrator
from app.options.models import ExpiryMode, OptionInstrument, OptionType, StrikeMode
from app.options.option_chain_service import OptionChainService
from app.options.risk import OptionRiskManager
from app.paper.dto import PaperOrderRequest, PaperOrderType
from app.paper.engine import PaperTradingEngine

_NEAR_EXPIRY = date(2026, 8, 7)
_T0 = datetime(2026, 7, 28, 5, 30, tzinfo=UTC)  # 11:00 IST -- safely inside market hours


class _RecordingLogger:
    """Mirrors `tests/unit/auto/test_orchestrator.py`'s hand-rolled log
    recorder -- see that file's own docstring for why `caplog`/
    `structlog.testing.capture_logs` are unreliable in this suite.
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
    monkeypatch.setattr(auto_trading_module, "logger", recorder)
    return recorder.entries


def _bars(closes: list[float]) -> list[HistoricalBar]:
    start = datetime(2024, 1, 1, 9, 15, tzinfo=UTC)
    bars = []
    for i, close in enumerate(closes):
        open_ = closes[i - 1] if i > 0 else close
        high = max(open_, close) * 1.01
        low = min(open_, close) * 0.99
        bars.append(
            HistoricalBar(
                timestamp=start + timedelta(minutes=5 * i),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=1000 + i,
            )
        )
    return bars


def _uptrend_closes(n: int = 60) -> list[float]:
    return [100 + i * 2.0 for i in range(n)]


def _downtrend_closes(n: int = 60) -> list[float]:
    return [100 + (n - i) * 2.0 for i in range(n)]


def _flat_closes(n: int = 60) -> list[float]:
    return [100.0 for _ in range(n)]


class FakeBroker:
    """A minimal `BrokerInterface` double exposing only `historical_data`/
    `ltp` -- everything `generate_option_recommendation`/
    `enter_option_position`/`exit_option_position` actually call.

    `ltp` returns whichever premium is currently set for the requested
    underlying (via `set_premium`), letting a test move a tracked
    position's "current premium" across a stop-loss/target boundary
    between cycles.
    """

    def __init__(self) -> None:
        self._bars_by_underlying: dict[str, list[HistoricalBar]] = {}
        self._premium_by_underlying: dict[str, float] = {}
        self.ltp_calls: list[str] = []

    def set_bars(self, underlying: str, bars: list[HistoricalBar]) -> None:
        self._bars_by_underlying[underlying] = bars

    def set_premium(self, underlying: str, premium: float) -> None:
        self._premium_by_underlying[underlying] = premium

    async def historical_data(self, exchange, tradingsymbol, interval, from_date, to_date):
        return self._bars_by_underlying[tradingsymbol]

    async def ltp(self, exchange: Exchange, tradingsymbol: str) -> Quote:
        self.ltp_calls.append(tradingsymbol)
        # An NFO option's tradingsymbol always starts with its underlying
        # (see `app.options.option_symbol_builder.OptionSymbolBuilder`) --
        # matching the longest configured underlying prefix mirrors
        # `app.options.paper_trading.get_option_trade_history`'s own
        # underlying-derivation approach.
        underlying = next(
            (u for u in sorted(self._premium_by_underlying, key=len, reverse=True)
             if tradingsymbol == u or tradingsymbol.startswith(u)),
            tradingsymbol,
        )
        premium = self._premium_by_underlying.get(underlying, 100.0)
        return Quote(tradingsymbol=tradingsymbol, exchange=exchange, last_price=premium)


class FakeOptionSource:
    def __init__(self) -> None:
        self.instruments: dict[str, list[OptionInstrument]] = {}

    async def fetch_option_instruments(self, underlying: str) -> list[OptionInstrument]:
        return self.instruments.get(underlying, [])


class FakeUnderlyingResolver:
    """A hand-written `UnderlyingResolver` fake -- identity mapping onto
    `Exchange.NSE`, matching `FakeBroker.historical_data`'s own
    `tradingsymbol`-keyed-by-underlying lookup above (so resolving
    `underlying` still fetches that same underlying's configured bars,
    rather than needing every test's `set_bars`/`set_premium` calls to be
    rewritten around a distinct resolved symbol).
    """

    async def resolve_historical_instrument(self, underlying: str) -> tuple[Exchange, str]:
        return (Exchange.NSE, underlying)


def _instrument(underlying: str, strike: float, option_type: OptionType) -> OptionInstrument:
    return OptionInstrument(
        underlying=underlying,
        expiry=_NEAR_EXPIRY,
        strike=strike,
        option_type=option_type,
        tradingsymbol=f"{underlying}{_NEAR_EXPIRY.strftime('%d%b%Y').upper()}{int(strike)}{option_type.value}",
        exchange=Exchange.NFO,
        token="1",
        lot_size=50,
    )


def _chain_service(underlyings: list[str]) -> tuple[OptionChainService, FakeOptionSource]:
    source = FakeOptionSource()
    for underlying in underlyings:
        instruments = []
        for strike in [23800.0, 23900.0, 24000.0, 24100.0, 24200.0]:
            instruments.append(_instrument(underlying, strike, OptionType.CE))
            instruments.append(_instrument(underlying, strike, OptionType.PE))
        source.instruments[underlying] = instruments
    service = OptionChainService(underlyings=underlyings, refresh_seconds=30.0, source=source)
    return service, source


def make_config(**overrides: object) -> AutoOptionsConfig:
    defaults: dict[str, object] = {
        "underlyings": ["NIFTY"],
        "timeframe": HistoricalInterval.FIVE_MINUTE,
        "strike_mode": StrikeMode.ATM,
        "expiry_mode": ExpiryMode.NEAREST_WEEKLY,
        "confidence_threshold": 0.0,
        "max_open_positions": 3,
        "lots_per_trade": 1,
        "scan_interval_seconds": 30.0,
        "exit_time": time(15, 20),
        "stop_loss_percent": 30.0,
        "target_percent": 50.0,
    }
    defaults.update(overrides)
    return AutoOptionsConfig(**defaults)  # type: ignore[arg-type]


def make_orchestrator(
    *,
    underlyings: list[str] | None = None,
    initial_capital: float = 1_000_000.0,
    session: MarketSessionState = MarketSessionState.OPEN,
    risk_manager: OptionRiskManager | None = None,
    **config_overrides: object,
) -> tuple[AutoOptionsOrchestrator, FakeBroker, PaperTradingEngine, OptionRiskManager]:
    underlyings = underlyings or ["NIFTY"]
    broker = FakeBroker()
    chain_service, _ = _chain_service(underlyings)
    engine = PaperTradingEngine(initial_capital=initial_capital)
    risk = risk_manager or OptionRiskManager(
        max_lots_per_order=10,
        max_premium_per_order=1_000_000.0,
        max_premium_exposure=1_000_000.0,
        max_daily_loss=1_000_000.0,
    )
    config = make_config(underlyings=underlyings, **config_overrides)
    orchestrator = AutoOptionsOrchestrator(
        broker=broker,
        broker_name=BrokerName.ANGEL_ONE,
        paper_engine=engine,
        option_chain_service=chain_service,
        underlying_resolver=FakeUnderlyingResolver(),
        risk_manager=risk,
        config=config,
        default_lot_size=50,
        max_lots_per_order=10,
        session_provider=lambda: session,
    )
    return orchestrator, broker, engine, risk


# -- entries ------------------------------------------------------------------------------


async def test_no_trade_recommendation_does_not_enter() -> None:
    orchestrator, broker, engine, _ = make_orchestrator()
    broker.set_bars("NIFTY", _bars(_flat_closes()))

    await orchestrator._run_cycle(now=_T0)

    assert orchestrator._positions == {}
    status = orchestrator.status()
    assert status.open_position_count == 0
    assert status.last_action == "scanning"


async def test_bullish_signal_opens_ce_position() -> None:
    orchestrator, broker, engine, _ = make_orchestrator()
    broker.set_bars("NIFTY", _bars(_uptrend_closes()))
    broker.set_premium("NIFTY", 100.0)

    await orchestrator._run_cycle(now=_T0)

    assert "NIFTY" in orchestrator._positions
    position = orchestrator._positions["NIFTY"]
    assert position.tradingsymbol.endswith("CE")
    assert position.entry_premium == 100.0
    assert position.stop_loss_premium == pytest.approx(70.0)
    assert position.target_premium == pytest.approx(150.0)
    status = orchestrator.status()
    assert status.open_position_count == 1
    assert status.trades_today == 1
    assert status.last_action == "trade_entered:NIFTY"


async def test_bearish_signal_opens_pe_position() -> None:
    orchestrator, broker, engine, _ = make_orchestrator()
    broker.set_bars("NIFTY", _bars(_downtrend_closes()))
    broker.set_premium("NIFTY", 100.0)

    await orchestrator._run_cycle(now=_T0)

    assert "NIFTY" in orchestrator._positions
    assert orchestrator._positions["NIFTY"].tradingsymbol.endswith("PE")


async def test_confidence_below_threshold_skips_entry() -> None:
    orchestrator, broker, engine, _ = make_orchestrator(confidence_threshold=200.0)
    broker.set_bars("NIFTY", _bars(_uptrend_closes()))
    broker.set_premium("NIFTY", 100.0)

    await orchestrator._run_cycle(now=_T0)

    assert orchestrator._positions == {}


async def test_existing_position_for_underlying_prevents_duplicate_entry() -> None:
    orchestrator, broker, engine, _ = make_orchestrator()
    broker.set_bars("NIFTY", _bars(_uptrend_closes()))
    broker.set_premium("NIFTY", 100.0)

    await orchestrator._run_cycle(now=_T0)
    first = orchestrator._positions["NIFTY"]

    await orchestrator._run_cycle(now=_T0)
    second = orchestrator._positions["NIFTY"]

    assert second.entry_order_id == first.entry_order_id
    assert orchestrator.status().trades_today == 1


async def test_max_open_positions_reached_skips_further_entries() -> None:
    orchestrator, broker, engine, _ = make_orchestrator(
        underlyings=["NIFTY", "BANKNIFTY"], max_open_positions=1
    )
    broker.set_bars("NIFTY", _bars(_uptrend_closes()))
    broker.set_premium("NIFTY", 100.0)
    broker.set_bars("BANKNIFTY", _bars(_uptrend_closes()))
    broker.set_premium("BANKNIFTY", 100.0)

    await orchestrator._run_cycle(now=_T0)

    assert len(orchestrator._positions) == 1


async def test_risk_rejection_prevents_entry_and_is_logged(
    orchestrator_logs: list[dict[str, object]],
) -> None:
    tiny_risk = OptionRiskManager(
        max_lots_per_order=10,
        max_premium_per_order=10.0,  # any real order value exceeds this
        max_premium_exposure=1_000_000.0,
        max_daily_loss=1_000_000.0,
    )
    orchestrator, broker, engine, _ = make_orchestrator(risk_manager=tiny_risk)
    broker.set_bars("NIFTY", _bars(_uptrend_closes()))
    broker.set_premium("NIFTY", 100.0)

    await orchestrator._run_cycle(now=_T0)

    assert orchestrator._positions == {}
    events = [entry["event"] for entry in orchestrator_logs]
    assert "auto_option_risk_rejection" in events


# -- exits -----------------------------------------------------------------------------------


async def _open_position(
    orchestrator: AutoOptionsOrchestrator, broker: FakeBroker, underlying: str = "NIFTY"
) -> None:
    broker.set_bars(underlying, _bars(_uptrend_closes()))
    broker.set_premium(underlying, 100.0)
    await orchestrator._run_cycle(now=_T0)
    assert underlying in orchestrator._positions


async def test_target_exit_closes_position_and_updates_realized_pnl(
    orchestrator_logs: list[dict[str, object]],
) -> None:
    orchestrator, broker, engine, risk = make_orchestrator()
    await _open_position(orchestrator, broker)

    broker.set_premium("NIFTY", 160.0)  # above the 150.0 target
    broker.set_bars("NIFTY", _bars(_flat_closes()))  # avoid a second entry this cycle

    await orchestrator._run_cycle(now=_T0)

    assert "NIFTY" not in orchestrator._positions
    assert risk.daily_realized_pnl == pytest.approx((160.0 - 100.0) * 50)
    events = [entry["event"] for entry in orchestrator_logs]
    assert "auto_option_target_exit" in events


async def test_stop_loss_exit_closes_position(
    orchestrator_logs: list[dict[str, object]],
) -> None:
    orchestrator, broker, engine, risk = make_orchestrator()
    await _open_position(orchestrator, broker)

    broker.set_premium("NIFTY", 60.0)  # below the 70.0 stop-loss
    broker.set_bars("NIFTY", _bars(_flat_closes()))

    await orchestrator._run_cycle(now=_T0)

    assert "NIFTY" not in orchestrator._positions
    assert risk.daily_realized_pnl == pytest.approx((60.0 - 100.0) * 50)
    events = [entry["event"] for entry in orchestrator_logs]
    assert "auto_option_stop_loss_exit" in events


async def test_market_close_force_exits_regardless_of_premium(
    orchestrator_logs: list[dict[str, object]],
) -> None:
    orchestrator, broker, engine, _ = make_orchestrator(exit_time=time(0, 0))
    await _open_position(orchestrator, broker)

    broker.set_premium("NIFTY", 100.0)  # unchanged; only the clock matters
    broker.set_bars("NIFTY", _bars(_flat_closes()))

    await orchestrator._run_cycle(now=_T0)

    assert "NIFTY" not in orchestrator._positions
    events = [entry["event"] for entry in orchestrator_logs]
    assert "auto_option_market_close_exit" in events


async def test_externally_closed_position_is_untracked_without_reexiting() -> None:
    orchestrator, broker, engine, _ = make_orchestrator()
    await _open_position(orchestrator, broker)
    position = orchestrator._positions["NIFTY"]

    # Simulate a manual close via the paper API directly (bypassing the orchestrator).
    await engine.place_order(
        PaperOrderRequest(
            symbol=position.tradingsymbol,
            exchange=Exchange.NFO,
            side=OrderSide.SELL,
            order_type=PaperOrderType.MARKET,
            quantity=position.quantity,
        ),
        current_price=100.0,
        now=_T0,
    )
    assert await engine.get_position(position.tradingsymbol, Exchange.NFO) is None

    broker.set_bars("NIFTY", _bars(_flat_closes()))
    await orchestrator._run_cycle(now=_T0)

    assert "NIFTY" not in orchestrator._positions


# -- lifecycle ----------------------------------------------------------------------------------


async def test_status_when_never_started() -> None:
    orchestrator, _, _, _ = make_orchestrator()

    status = orchestrator.status()

    assert status.running is False
    assert status.started_at is None
    assert status.cycle_count == 0
    assert status.next_scan_at is None
    assert status.last_action == "idle"


async def test_start_marks_running_and_stop_clears_it() -> None:
    orchestrator, broker, _, _ = make_orchestrator(scan_interval_seconds=0.05)
    broker.set_bars("NIFTY", _bars(_flat_closes()))

    await orchestrator.start()
    assert orchestrator.status().running is True

    await orchestrator.stop()
    assert orchestrator.status().running is False


async def test_start_is_idempotent() -> None:
    orchestrator, broker, _, _ = make_orchestrator(scan_interval_seconds=0.05)
    broker.set_bars("NIFTY", _bars(_flat_closes()))

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


async def test_next_scan_at_computed_only_while_running() -> None:
    orchestrator, broker, _, _ = make_orchestrator(scan_interval_seconds=0.05)
    broker.set_bars("NIFTY", _bars(_flat_closes()))

    await orchestrator.start()
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if orchestrator.status().last_scan_at is not None:
                break
        status = orchestrator.status()
        assert status.next_scan_at is not None
        assert status.last_scan_at is not None
        assert status.next_scan_at > status.last_scan_at
    finally:
        await orchestrator.stop()

    assert orchestrator.status().next_scan_at is None


async def test_session_closed_gating_never_runs_a_cycle() -> None:
    orchestrator, broker, _, _ = make_orchestrator(
        scan_interval_seconds=0.02, session=MarketSessionState.CLOSED
    )
    broker.set_bars("NIFTY", _bars(_flat_closes()))

    await orchestrator.start()
    await asyncio.sleep(0.1)
    await orchestrator.stop()

    assert orchestrator.status().cycle_count == 0
    assert broker.ltp_calls == []
