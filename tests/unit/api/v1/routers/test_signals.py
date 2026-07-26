"""Unit tests for `app.api.v1.routers.signals`."""

from datetime import UTC, datetime, timedelta
from unittest.mock import create_autospec

from fastapi.testclient import TestClient

from app.api.v1.routers.signals import get_broker
from app.brokers.base import BrokerInterface
from app.domain.entities.broker import HistoricalBar
from app.domain.exceptions.market import NoHistoricalDataError
from app.main import create_app


def _bars(closes: list[float]) -> list[HistoricalBar]:
    start = datetime(2024, 1, 1, 9, 15, tzinfo=UTC)
    bars = []
    for i, close in enumerate(closes):
        open_ = closes[i - 1] if i > 0 else close
        high = max(open_, close) * 1.002
        low = min(open_, close) * 0.998
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


def _client_with_mock_broker(broker: object) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_broker] = lambda: broker
    return TestClient(app)


def test_signal_endpoint_returns_a_recommendation_for_a_real_uptrend() -> None:
    broker = create_autospec(BrokerInterface, instance=True)
    broker.historical_data.return_value = _bars([100 + i * 0.5 for i in range(60)])
    client = _client_with_mock_broker(broker)

    response = client.get(
        "/api/v1/signals",
        params={"exchange": "NSE", "tradingsymbol": "256265", "interval": "5minute"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "256265"
    assert body["exchange"] == "NSE"
    assert body["action"] in {"BUY", "SELL", "HOLD"}
    assert 0 <= body["confidence"] <= 100
    assert body["total_strategy_count"] == 3
    assert isinstance(body["reasoning"], str) and body["reasoning"]


def test_signal_endpoint_defaults_interval_to_five_minute() -> None:
    broker = create_autospec(BrokerInterface, instance=True)
    broker.historical_data.return_value = _bars([100 + i * 0.5 for i in range(60)])
    client = _client_with_mock_broker(broker)

    response = client.get("/api/v1/signals", params={"exchange": "NSE", "tradingsymbol": "256265"})

    assert response.status_code == 200
    assert response.json()["timeframe"] == "5minute"
    call_args = broker.historical_data.call_args
    assert call_args.args[2].value == "5minute"


def test_signal_endpoint_maps_no_historical_data_to_404() -> None:
    from app.domain.enums.trading import BrokerName, Exchange, HistoricalInterval

    broker = create_autospec(BrokerInterface, instance=True)
    broker.historical_data.side_effect = NoHistoricalDataError(
        broker=BrokerName.ZERODHA,
        exchange=Exchange.NSE,
        tradingsymbol="MADEUP",
        interval=HistoricalInterval.FIVE_MINUTE,
    )
    client = _client_with_mock_broker(broker)

    response = client.get("/api/v1/signals", params={"exchange": "NSE", "tradingsymbol": "MADEUP"})

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NoHistoricalDataError"


def test_signal_endpoint_requires_tradingsymbol() -> None:
    broker = create_autospec(BrokerInterface, instance=True)
    client = _client_with_mock_broker(broker)

    response = client.get("/api/v1/signals", params={"exchange": "NSE"})

    assert response.status_code == 422


def test_signal_endpoint_rejects_unknown_exchange() -> None:
    broker = create_autospec(BrokerInterface, instance=True)
    client = _client_with_mock_broker(broker)

    response = client.get(
        "/api/v1/signals", params={"exchange": "NOT_REAL", "tradingsymbol": "256265"}
    )

    assert response.status_code == 422
