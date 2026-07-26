"""Unit tests for `app.core.exception_handlers`."""

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from app.core.exception_handlers import register_exception_handlers
from app.domain.enums.trading import BrokerName, Exchange, HistoricalInterval
from app.domain.exceptions.broker import (
    BrokerAPIError,
    BrokerAuthenticationError,
    BrokerConnectionError,
    OrderRejectionError,
    WebSocketError,
)
from app.domain.exceptions.indicators import InsufficientDataError, UnknownIndicatorError
from app.domain.exceptions.market import InvalidHistoricalDataError, NoHistoricalDataError
from app.domain.exceptions.paper import (
    InsufficientCashError,
    InvalidOrderStateError,
    OrderNotFoundError,
    PaperTradingError,
)
from app.domain.exceptions.research import FeatureEngineeringError, UnknownRunError


def _raise(exc: Exception) -> Callable[[], None]:
    def _handler() -> None:
        raise exc

    return _handler


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    app.get("/broker-connection")(
        _raise(BrokerConnectionError("could not reach broker", broker=BrokerName.ZERODHA))
    )
    app.get("/broker-auth")(
        _raise(BrokerAuthenticationError("bad credentials", broker=BrokerName.ANGEL_ONE))
    )
    app.get("/broker-api")(
        _raise(
            BrokerAPIError(
                "invalid instrument",
                broker=BrokerName.UPSTOX,
                status_code=400,
                error_code="INVALID_INSTRUMENT",
            )
        )
    )
    app.get("/order-rejected")(
        _raise(
            OrderRejectionError(
                "insufficient margin",
                broker=BrokerName.ZERODHA,
                status_code=400,
                error_code="MARGIN_EXCEEDED",
            )
        )
    )
    app.get("/websocket-error")(_raise(WebSocketError("streaming exhausted its reconnect policy")))
    app.get("/unknown-indicator")(
        _raise(UnknownIndicatorError("MADE_UP", available=["RSI", "EMA"]))
    )
    app.get("/insufficient-data")(_raise(InsufficientDataError("RSI")))
    app.get("/feature-engineering")(_raise(FeatureEngineeringError("cannot compute feature")))
    app.get("/unknown-run")(_raise(UnknownRunError("run-123")))
    app.get("/no-historical-data")(
        _raise(
            NoHistoricalDataError(
                broker=BrokerName.ZERODHA,
                exchange=Exchange.NSE,
                tradingsymbol="256265",
                interval=HistoricalInterval.FIVE_MINUTE,
            )
        )
    )
    app.get("/invalid-historical-data")(
        _raise(
            InvalidHistoricalDataError(
                broker=BrokerName.UPSTOX,
                exchange=Exchange.NSE,
                tradingsymbol="NSE_EQ|X",
                reason="high is below low",
            )
        )
    )
    app.get("/order-not-found")(_raise(OrderNotFoundError("order-123")))
    app.get("/invalid-order-state")(_raise(InvalidOrderStateError("order-123", "COMPLETE")))
    app.get("/insufficient-cash")(
        _raise(
            InsufficientCashError(
                "paper: insufficient cash: requested 1000.0, available 500.0",
                requested=1000.0,
                available=500.0,
            )
        )
    )
    app.get("/paper-trading-error")(_raise(PaperTradingError("some other paper trading failure")))
    app.get("/unexpected")(_raise(ValueError("some internal detail nobody should see")))

    return TestClient(app, raise_server_exceptions=False)


def test_broker_connection_error_maps_to_503(client: TestClient) -> None:
    response = client.get("/broker-connection")

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["type"] == "BrokerConnectionError"
    assert body["error"]["message"] == "could not reach broker"
    assert body["error"]["detail"]["broker"] == "zerodha"


def test_broker_authentication_error_falls_back_to_broker_error_502(client: TestClient) -> None:
    response = client.get("/broker-auth")

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "BrokerAuthenticationError"


def test_broker_api_error_maps_to_502_with_error_code_detail(client: TestClient) -> None:
    response = client.get("/broker-api")

    assert response.status_code == 502
    detail = response.json()["error"]["detail"]
    assert detail["error_code"] == "INVALID_INSTRUMENT"
    assert detail["status_code"] == 400
    assert detail["broker"] == "upstox"


def test_order_rejection_error_maps_to_422_not_502(client: TestClient) -> None:
    response = client.get("/order-rejected")

    assert response.status_code == 422
    assert response.json()["error"]["type"] == "OrderRejectionError"


def test_websocket_error_maps_to_503(client: TestClient) -> None:
    response = client.get("/websocket-error")

    assert response.status_code == 503


def test_unknown_indicator_error_maps_to_400_with_available_detail(client: TestClient) -> None:
    response = client.get("/unknown-indicator")

    assert response.status_code == 400
    detail = response.json()["error"]["detail"]
    assert detail["requested"] == "MADE_UP"
    assert detail["available"] == ["RSI", "EMA"]


def test_insufficient_data_error_maps_to_422_not_400(client: TestClient) -> None:
    response = client.get("/insufficient-data")

    assert response.status_code == 422
    assert response.json()["error"]["detail"]["indicator_name"] == "RSI"


def test_feature_engineering_error_falls_back_to_research_error_422(client: TestClient) -> None:
    response = client.get("/feature-engineering")

    assert response.status_code == 422
    assert response.json()["error"]["type"] == "FeatureEngineeringError"


def test_unknown_run_error_maps_to_404_not_422(client: TestClient) -> None:
    response = client.get("/unknown-run")

    assert response.status_code == 404
    assert response.json()["error"]["detail"]["run_id"] == "run-123"


def test_no_historical_data_error_maps_to_404(client: TestClient) -> None:
    response = client.get("/no-historical-data")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["type"] == "NoHistoricalDataError"
    detail = body["error"]["detail"]
    assert detail["broker"] == "zerodha"
    assert detail["tradingsymbol"] == "256265"


def test_invalid_historical_data_error_maps_to_502_not_market_data_error_default(
    client: TestClient,
) -> None:
    response = client.get("/invalid-historical-data")

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["type"] == "InvalidHistoricalDataError"
    assert body["error"]["detail"]["reason"] == "high is below low"


def test_order_not_found_error_maps_to_404(client: TestClient) -> None:
    response = client.get("/order-not-found")

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "OrderNotFoundError"


def test_invalid_order_state_error_maps_to_409(client: TestClient) -> None:
    response = client.get("/invalid-order-state")

    assert response.status_code == 409
    detail = response.json()["error"]["detail"]
    assert detail["order_id"] == "order-123"
    assert detail["status"] == "COMPLETE"


def test_insufficient_cash_error_maps_to_422(client: TestClient) -> None:
    response = client.get("/insufficient-cash")

    assert response.status_code == 422
    detail = response.json()["error"]["detail"]
    assert detail["requested"] == 1000.0
    assert detail["available"] == 500.0


def test_paper_trading_error_falls_back_to_400(client: TestClient) -> None:
    response = client.get("/paper-trading-error")

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "PaperTradingError"


def test_unexpected_exception_maps_to_500_without_leaking_message(client: TestClient) -> None:
    response = client.get("/unexpected")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["type"] == "InternalServerError"
    assert "some internal detail nobody should see" not in response.text
    assert body["error"]["message"] == "An unexpected error occurred."


def test_domain_error_is_logged_with_structured_fields(client: TestClient) -> None:
    with capture_logs() as logs:
        client.get("/unknown-run")

    domain_logs = [entry for entry in logs if entry["event"] == "domain_error"]
    assert len(domain_logs) == 1
    assert domain_logs[0]["exception_type"] == "UnknownRunError"
    assert domain_logs[0]["http_status_code"] == 404
    assert domain_logs[0]["method"] == "GET"
    assert domain_logs[0]["path"] == "/unknown-run"
    assert domain_logs[0]["detail"]["run_id"] == "run-123"


def test_unexpected_exception_is_logged_with_full_detail_server_side(client: TestClient) -> None:
    with capture_logs() as logs:
        client.get("/unexpected")

    error_logs = [entry for entry in logs if entry["event"] == "unhandled_exception"]
    assert len(error_logs) == 1
    assert error_logs[0]["exception_type"] == "ValueError"
    assert error_logs[0]["log_level"] == "error"


def test_fastapi_builtin_404_is_not_hijacked_by_generic_handler(client: TestClient) -> None:
    response = client.get("/this-route-does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
