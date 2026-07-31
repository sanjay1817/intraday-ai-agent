"""Unit tests for `app.api.v1.routers.options`.

Mirrors `test_signals.py`'s `TestClient` + `dependency_overrides` style:
a fake `BrokerInterface` (via `create_autospec`) and a real
`OptionChainService` fronting a hand-written fake `OptionInstrumentSource`
(same as `tests/unit/options/test_option_chain_service.py`), with
`Settings` overridden per test via `get_settings`.
"""

from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import create_autospec

import pytest
from fastapi.testclient import TestClient

from app.api.v1.routers.options import (
    get_auto_options_orchestrator_or_error,
    get_option_chain_service_or_error,
    get_option_risk_manager_or_error,
    get_underlying_resolver_or_error,
)
from app.api.v1.routers.paper import get_paper_engine
from app.api.v1.routers.signals import get_broker
from app.brokers.base import BrokerInterface
from app.config.settings import Settings, get_settings
from app.domain.entities.broker import HistoricalBar, Quote
from app.domain.enums.trading import BrokerName, Exchange, HistoricalInterval
from app.domain.exceptions.broker import BrokerAPIError
from app.main import create_app
from app.options.auto_trading import AutoOptionsConfig, AutoOptionsOrchestrator
from app.options.models import ExpiryMode, OptionInstrument, OptionType, StrikeMode
from app.options.option_chain_service import OptionChainService
from app.options.risk import OptionRiskManager
from app.options.underlying_resolver import UnderlyingResolver
from app.paper.engine import PaperTradingEngine

_NEAR_EXPIRY = date(2026, 8, 7)

#: Distinguishes "caller passed no `underlying_resolver` override" (use the
#: default fake) from "caller explicitly passed `None`" (leave
#: `app.state.underlying_resolver` as `create_app()`'s own default, e.g.
#: for the dedicated 503 test below) -- `None` alone can't disambiguate
#: those two cases.
_UNSET = object()


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


def _instrument(underlying: str, expiry: date, strike: float, option_type: OptionType) -> OptionInstrument:
    return OptionInstrument(
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        tradingsymbol=f"{underlying}{expiry.strftime('%d%b%Y').upper()}{int(strike)}{option_type.value}",
        exchange=Exchange.NFO,
        token="1",
        lot_size=50,
    )


class FakeOptionSource:
    def __init__(self) -> None:
        self.instruments: dict[str, list[OptionInstrument]] = {}

    async def fetch_option_instruments(self, underlying: str) -> list[OptionInstrument]:
        return self.instruments.get(underlying, [])


class FakeUnderlyingResolver:
    """A hand-written `UnderlyingResolver` fake -- identity mapping onto
    `Exchange.NSE`, since `_broker_with_uptrend`'s `historical_data` mock
    ignores the tradingsymbol it's called with anyway; only its presence
    (not its exact resolved value) matters for these router tests.
    """

    async def resolve_historical_instrument(self, underlying: str) -> tuple[Exchange, str]:
        return (Exchange.NSE, underlying)


def _underlying_resolver() -> UnderlyingResolver:
    return FakeUnderlyingResolver()


def _nifty_chain_service() -> OptionChainService:
    source = FakeOptionSource()
    instruments = []
    for strike in [23800.0, 23900.0, 24000.0, 24100.0, 24200.0]:
        instruments.append(_instrument("NIFTY", _NEAR_EXPIRY, strike, OptionType.CE))
        instruments.append(_instrument("NIFTY", _NEAR_EXPIRY, strike, OptionType.PE))
    source.instruments["NIFTY"] = instruments
    return OptionChainService(underlyings=["NIFTY", "BANKNIFTY"], refresh_seconds=30.0, source=source)


def _options_settings(**overrides: object) -> Settings:
    kwargs: dict = {
        "trading_mode": "OPTIONS",
        "option_underlyings": ["NIFTY", "BANKNIFTY"],
        "broker": {},
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def _client(
    broker: object,
    *,
    option_chain_service: object | None,
    settings: Settings,
    risk_manager: object | None = None,
    paper_engine: object | None = None,
    underlying_resolver: object | None = _UNSET,
) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_broker] = lambda: broker
    app.dependency_overrides[get_settings] = lambda: settings
    if option_chain_service is not None:
        app.dependency_overrides[get_option_chain_service_or_error] = lambda: option_chain_service
    if risk_manager is not None:
        app.dependency_overrides[get_option_risk_manager_or_error] = lambda: risk_manager
    if paper_engine is not None:
        app.dependency_overrides[get_paper_engine] = lambda: paper_engine
    resolved_underlying_resolver = _underlying_resolver() if underlying_resolver is _UNSET else underlying_resolver
    if resolved_underlying_resolver is not None:
        app.dependency_overrides[get_underlying_resolver_or_error] = lambda: resolved_underlying_resolver
    return TestClient(app)


def _broker_with_uptrend(premium: float = 42.5) -> object:
    broker = create_autospec(BrokerInterface, instance=True)
    broker.historical_data.return_value = _bars(_uptrend_closes())

    async def _ltp(exchange, tradingsymbol):
        return Quote(tradingsymbol=tradingsymbol, exchange=exchange, last_price=premium)

    broker.ltp.side_effect = _ltp
    return broker


def test_recommendation_endpoint_happy_path_returns_200_with_populated_recommendation() -> None:
    broker = _broker_with_uptrend()
    client = _client(
        broker,
        option_chain_service=_nifty_chain_service(),
        settings=_options_settings(),
    )

    response = client.get("/api/v1/options/recommendation", params={"underlying": "NIFTY"})

    assert response.status_code == 200
    body = response.json()
    assert body["underlying"] == "NIFTY"
    assert body["signal"] in {"BULLISH", "BEARISH"}
    assert body["tradingsymbol"] is not None
    assert body["premium"] == 42.5


def test_recommendation_endpoint_rejects_when_trading_mode_not_options() -> None:
    broker = _broker_with_uptrend()
    client = _client(
        broker,
        option_chain_service=_nifty_chain_service(),
        settings=_options_settings(trading_mode="EQUITY"),
    )

    response = client.get("/api/v1/options/recommendation", params={"underlying": "NIFTY"})

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "TradingModeNotEnabledError"


def test_recommendation_endpoint_rejects_unsupported_underlying() -> None:
    broker = _broker_with_uptrend()
    client = _client(
        broker,
        option_chain_service=_nifty_chain_service(),
        settings=_options_settings(),
    )

    response = client.get("/api/v1/options/recommendation", params={"underlying": "SENSEX"})

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "UnsupportedUnderlyingError"


def test_recommendation_endpoint_returns_503_when_option_chain_service_is_none() -> None:
    app = create_app()
    broker = _broker_with_uptrend()
    app.dependency_overrides[get_broker] = lambda: broker
    app.dependency_overrides[get_settings] = lambda: _options_settings()
    app.state.option_chain_service = None
    client = TestClient(app)

    response = client.get("/api/v1/options/recommendation", params={"underlying": "NIFTY"})

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "OptionsInfrastructureUnavailableError"


def test_recommendation_endpoint_returns_503_when_underlying_resolver_is_none() -> None:
    app = create_app()
    broker = _broker_with_uptrend()
    app.dependency_overrides[get_broker] = lambda: broker
    app.dependency_overrides[get_settings] = lambda: _options_settings()
    app.dependency_overrides[get_option_chain_service_or_error] = lambda: _nifty_chain_service()
    app.state.underlying_resolver = None
    client = TestClient(app)

    response = client.get("/api/v1/options/recommendation", params={"underlying": "NIFTY"})

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "OptionsInfrastructureUnavailableError"


def test_recommendation_endpoint_maps_premium_fetch_failure_to_502() -> None:
    broker = create_autospec(BrokerInterface, instance=True)
    broker.historical_data.return_value = _bars(_uptrend_closes())
    broker.ltp.side_effect = BrokerAPIError("bad symbol", status_code=400)
    client = _client(
        broker,
        option_chain_service=_nifty_chain_service(),
        settings=_options_settings(),
    )

    response = client.get("/api/v1/options/recommendation", params={"underlying": "NIFTY"})

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "PremiumUnavailableError"


# -- POST /api/v1/options/paper/orders -------------------------------------------------------


def _risk_manager(**overrides: object) -> OptionRiskManager:
    kwargs: dict = {
        "max_lots_per_order": 10,
        "max_premium_per_order": 1_000_000.0,
        "max_premium_exposure": 1_000_000.0,
        "max_daily_loss": 1_000_000.0,
    }
    kwargs.update(overrides)
    return OptionRiskManager(**kwargs)  # type: ignore[arg-type]


def test_place_option_paper_order_happy_path_places_a_paper_order() -> None:
    broker = _broker_with_uptrend(premium=100.0)
    client = _client(
        broker,
        option_chain_service=_nifty_chain_service(),
        settings=_options_settings(),
        risk_manager=_risk_manager(),
        paper_engine=PaperTradingEngine(initial_capital=1_000_000.0),
    )

    response = client.post(
        "/api/v1/options/paper/orders",
        json={"underlying": "NIFTY", "lots": 1},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["recommendation"]["signal"] in {"BULLISH", "BEARISH"}
    assert body["order"] is not None
    assert body["order"]["status"] == "COMPLETE"
    assert body["order"]["exchange"] == "NFO"


def test_place_option_paper_order_no_trade_returns_null_order() -> None:
    broker = create_autospec(BrokerInterface, instance=True)
    broker.historical_data.return_value = _bars([100.0 for _ in range(60)])  # flat -> HOLD
    client = _client(
        broker,
        option_chain_service=_nifty_chain_service(),
        settings=_options_settings(),
        risk_manager=_risk_manager(),
        paper_engine=PaperTradingEngine(initial_capital=1_000_000.0),
    )

    response = client.post(
        "/api/v1/options/paper/orders",
        json={"underlying": "NIFTY", "lots": 1},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["recommendation"]["signal"] == "NO_TRADE"
    assert body["order"] is None


def test_place_option_paper_order_rejects_when_trading_mode_not_options() -> None:
    broker = _broker_with_uptrend()
    client = _client(
        broker,
        option_chain_service=_nifty_chain_service(),
        settings=_options_settings(trading_mode="EQUITY"),
        risk_manager=_risk_manager(),
        paper_engine=PaperTradingEngine(),
    )

    response = client.post(
        "/api/v1/options/paper/orders",
        json={"underlying": "NIFTY", "lots": 1},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "TradingModeNotEnabledError"


def test_place_option_paper_order_lots_exceeding_max_returns_400() -> None:
    broker = _broker_with_uptrend(premium=100.0)
    client = _client(
        broker,
        option_chain_service=_nifty_chain_service(),
        settings=_options_settings(option_max_lots_per_order=1),
        risk_manager=_risk_manager(max_lots_per_order=1),
        paper_engine=PaperTradingEngine(initial_capital=1_000_000.0),
    )

    response = client.post(
        "/api/v1/options/paper/orders",
        json={"underlying": "NIFTY", "lots": 2},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "InvalidLotQuantityError"


def test_place_option_paper_order_risk_limit_breach_returns_422() -> None:
    broker = _broker_with_uptrend(premium=100.0)
    client = _client(
        broker,
        option_chain_service=_nifty_chain_service(),
        settings=_options_settings(),
        risk_manager=_risk_manager(max_premium_per_order=10.0),
        paper_engine=PaperTradingEngine(initial_capital=1_000_000.0),
    )

    response = client.post(
        "/api/v1/options/paper/orders",
        json={"underlying": "NIFTY", "lots": 1},
    )

    assert response.status_code == 422
    assert response.json()["error"]["type"] == "OptionRiskLimitExceededError"


# -- POST /api/v1/options/paper/exit ---------------------------------------------------------


def test_exit_option_paper_order_happy_path() -> None:
    broker = _broker_with_uptrend(premium=100.0)
    engine = PaperTradingEngine(initial_capital=1_000_000.0)
    client = _client(
        broker,
        option_chain_service=_nifty_chain_service(),
        settings=_options_settings(),
        risk_manager=_risk_manager(),
        paper_engine=engine,
    )

    place_response = client.post(
        "/api/v1/options/paper/orders",
        json={"underlying": "NIFTY", "lots": 1},
    )
    tradingsymbol = place_response.json()["order"]["symbol"]

    response = client.post(
        "/api/v1/options/paper/exit",
        json={"tradingsymbol": tradingsymbol, "reason": "MANUAL"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETE"


def test_exit_option_paper_order_no_position_returns_404() -> None:
    broker = _broker_with_uptrend()
    client = _client(
        broker,
        option_chain_service=_nifty_chain_service(),
        settings=_options_settings(),
        risk_manager=_risk_manager(),
        paper_engine=PaperTradingEngine(),
    )

    response = client.post(
        "/api/v1/options/paper/exit",
        json={"tradingsymbol": "NIFTY07AUG202624000CE", "reason": "MANUAL"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "OptionPositionNotFoundError"


def test_exit_option_paper_order_rejects_when_trading_mode_not_options() -> None:
    broker = _broker_with_uptrend()
    client = _client(
        broker,
        option_chain_service=_nifty_chain_service(),
        settings=_options_settings(trading_mode="EQUITY"),
        risk_manager=_risk_manager(),
        paper_engine=PaperTradingEngine(),
    )

    response = client.post(
        "/api/v1/options/paper/exit",
        json={"tradingsymbol": "NIFTY07AUG202624000CE", "reason": "MANUAL"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "TradingModeNotEnabledError"


# -- GET /api/v1/options/paper/trades --------------------------------------------------------


def test_get_option_paper_trades_reflects_a_closed_position() -> None:
    broker = _broker_with_uptrend(premium=100.0)
    engine = PaperTradingEngine(initial_capital=1_000_000.0)
    client = _client(
        broker,
        option_chain_service=_nifty_chain_service(),
        settings=_options_settings(),
        risk_manager=_risk_manager(),
        paper_engine=engine,
    )

    place_response = client.post(
        "/api/v1/options/paper/orders",
        json={"underlying": "NIFTY", "lots": 1},
    )
    tradingsymbol = place_response.json()["order"]["symbol"]
    client.post(
        "/api/v1/options/paper/exit",
        json={"tradingsymbol": tradingsymbol, "reason": "MANUAL"},
    )

    response = client.get("/api/v1/options/paper/trades")

    assert response.status_code == 200
    trades = response.json()
    assert len(trades) == 1
    assert trades[0]["underlying"] == "NIFTY"
    assert trades[0]["tradingsymbol"] == tradingsymbol
    assert trades[0]["reason"] == "MANUAL"


def test_get_option_paper_trades_rejects_when_trading_mode_not_options() -> None:
    broker = _broker_with_uptrend()
    client = _client(
        broker,
        option_chain_service=_nifty_chain_service(),
        settings=_options_settings(trading_mode="EQUITY"),
        risk_manager=_risk_manager(),
        paper_engine=PaperTradingEngine(),
    )

    response = client.get("/api/v1/options/paper/trades")

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "TradingModeNotEnabledError"


# -- GET /api/v1/options/risk/status ----------------------------------------------------------


def test_get_option_risk_status_happy_path_reflects_configured_limits_and_exposure() -> None:
    broker = _broker_with_uptrend(premium=100.0)
    engine = PaperTradingEngine(initial_capital=1_000_000.0)
    client = _client(
        broker,
        option_chain_service=_nifty_chain_service(),
        settings=_options_settings(
            option_max_premium_exposure=1_000_000.0,
            option_max_daily_loss=10_000.0,
            option_max_lots_per_order=10,
            option_max_premium_per_order=1_000_000.0,
        ),
        risk_manager=_risk_manager(),
        paper_engine=engine,
    )

    client.post("/api/v1/options/paper/orders", json={"underlying": "NIFTY", "lots": 1})

    response = client.get("/api/v1/options/risk/status")

    assert response.status_code == 200
    body = response.json()
    assert body["current_premium_exposure"] == pytest.approx(50 * 100.0)
    assert body["max_premium_exposure"] == 1_000_000.0
    assert body["remaining_premium_capacity"] == pytest.approx(1_000_000.0 - 50 * 100.0)
    assert body["daily_realized_pnl"] == 0.0
    assert body["max_daily_loss"] == 10_000.0
    assert body["max_lots_per_order"] == 10
    assert body["max_premium_per_order"] == 1_000_000.0


def test_get_option_risk_status_rejects_when_trading_mode_not_options() -> None:
    broker = _broker_with_uptrend()
    client = _client(
        broker,
        option_chain_service=_nifty_chain_service(),
        settings=_options_settings(trading_mode="EQUITY"),
        risk_manager=_risk_manager(),
        paper_engine=PaperTradingEngine(),
    )

    response = client.get("/api/v1/options/risk/status")

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "TradingModeNotEnabledError"


def test_get_option_risk_status_returns_503_when_risk_manager_is_none() -> None:
    app = create_app()
    broker = _broker_with_uptrend()
    app.dependency_overrides[get_broker] = lambda: broker
    app.dependency_overrides[get_settings] = lambda: _options_settings()
    app.dependency_overrides[get_paper_engine] = lambda: PaperTradingEngine()
    app.state.option_risk_manager = None
    client = TestClient(app)

    response = client.get("/api/v1/options/risk/status")

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "OptionsInfrastructureUnavailableError"


# -- GET/POST /api/v1/options/auto/* (Phase 5) ------------------------------------------------


def _auto_options_orchestrator() -> AutoOptionsOrchestrator:
    broker = _broker_with_uptrend()
    chain_service = _nifty_chain_service()
    return AutoOptionsOrchestrator(
        broker=broker,
        broker_name=BrokerName.ANGEL_ONE,
        paper_engine=PaperTradingEngine(initial_capital=1_000_000.0),
        option_chain_service=chain_service,
        underlying_resolver=_underlying_resolver(),
        risk_manager=_risk_manager(),
        config=AutoOptionsConfig(
            underlyings=["NIFTY"],
            timeframe=HistoricalInterval.FIVE_MINUTE,
            strike_mode=StrikeMode.ATM,
            expiry_mode=ExpiryMode.NEAREST_WEEKLY,
            confidence_threshold=60.0,
            max_open_positions=3,
            lots_per_trade=1,
            scan_interval_seconds=30.0,
            exit_time=time(15, 20),
            stop_loss_percent=30.0,
            target_percent=50.0,
        ),
        default_lot_size=50,
        max_lots_per_order=10,
    )


def _auto_client(orchestrator: object, settings: Settings) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_auto_options_orchestrator_or_error] = lambda: orchestrator
    return TestClient(app)


def test_get_auto_options_status_happy_path() -> None:
    orchestrator = _auto_options_orchestrator()
    client = _auto_client(orchestrator, _options_settings())

    response = client.get("/api/v1/options/auto/status")

    assert response.status_code == 200
    body = response.json()
    assert body["running"] is False
    assert body["open_position_count"] == 0


def test_get_auto_options_status_rejects_when_trading_mode_not_options() -> None:
    orchestrator = _auto_options_orchestrator()
    client = _auto_client(orchestrator, _options_settings(trading_mode="EQUITY"))

    response = client.get("/api/v1/options/auto/status")

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "TradingModeNotEnabledError"


def test_get_auto_options_status_returns_503_when_orchestrator_is_none() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: _options_settings()
    app.state.auto_options_orchestrator = None
    client = TestClient(app)

    response = client.get("/api/v1/options/auto/status")

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "OptionsInfrastructureUnavailableError"


def test_start_auto_options_trading_happy_path() -> None:
    orchestrator = _auto_options_orchestrator()
    client = _auto_client(orchestrator, _options_settings())

    response = client.post("/api/v1/options/auto/start")

    assert response.status_code == 200
    assert response.json()["running"] is True


def test_start_auto_options_trading_rejects_when_trading_mode_not_options() -> None:
    orchestrator = _auto_options_orchestrator()
    client = _auto_client(orchestrator, _options_settings(trading_mode="EQUITY"))

    response = client.post("/api/v1/options/auto/start")

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "TradingModeNotEnabledError"
    assert orchestrator.is_running is False


def test_stop_auto_options_trading_happy_path() -> None:
    orchestrator = _auto_options_orchestrator()
    client = _auto_client(orchestrator, _options_settings())
    client.post("/api/v1/options/auto/start")

    response = client.post("/api/v1/options/auto/stop")

    assert response.status_code == 200
    assert response.json()["running"] is False


def test_stop_auto_options_trading_returns_503_when_orchestrator_is_none() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: _options_settings()
    app.state.auto_options_orchestrator = None
    client = TestClient(app)

    response = client.post("/api/v1/options/auto/stop")

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "OptionsInfrastructureUnavailableError"
