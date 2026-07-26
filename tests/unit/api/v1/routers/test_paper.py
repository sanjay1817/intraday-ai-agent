"""Unit tests for `app.api.v1.routers.paper`.

Uses a real `PaperTradingEngine` (already thoroughly unit-tested on its
own) wired in via dependency override, and a mocked `BrokerInterface`
for LTP price discovery — so these tests exercise the router's own
request/response translation and wiring, not re-verify matching logic
already covered elsewhere.
"""

from unittest.mock import create_autospec

from fastapi.testclient import TestClient

from app.api.v1.routers.paper import get_broker, get_paper_engine
from app.brokers.base import BrokerInterface
from app.domain.entities.broker import Quote
from app.domain.enums.trading import Exchange
from app.domain.exceptions.broker import BrokerConnectionError
from app.main import create_app
from app.paper.engine import PaperTradingEngine


def make_client(*, ltp: float = 1500.0, initial_capital: float = 100_000.0) -> TestClient:
    app = create_app()
    broker = create_autospec(BrokerInterface, instance=True)
    broker.ltp.return_value = Quote(tradingsymbol="INFY-EQ", exchange=Exchange.NSE, last_price=ltp)
    # Constructed once, outside the override callable: FastAPI invokes a
    # dependency override fresh on every request, so a lambda that builds
    # a new PaperTradingEngine() each time would silently discard state
    # between requests instead of sharing one paper account across them.
    engine = PaperTradingEngine(initial_capital=initial_capital)
    app.dependency_overrides[get_broker] = lambda: broker
    app.dependency_overrides[get_paper_engine] = lambda: engine
    return TestClient(app)


def market_order_body(
    symbol: str = "INFY-EQ", side: str = "BUY", quantity: int = 10, **overrides: object
) -> dict[str, object]:
    order: dict[str, object] = {
        "symbol": symbol,
        "exchange": "NSE",
        "side": side,
        "order_type": "MARKET",
        "quantity": quantity,
    }
    order.update(overrides)
    return {"order": order}


# -- POST /api/v1/paper/order --------------------------------------------------------------


def test_place_market_order_fills_immediately() -> None:
    client = make_client(ltp=1500.0)

    response = client.post("/api/v1/paper/order", json=market_order_body())

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "COMPLETE"
    assert body["filled_quantity"] == 10
    assert body["average_fill_price"] == 1500.0


def test_place_limit_order_that_is_not_marketable_rests_open() -> None:
    client = make_client(ltp=1500.0)

    response = client.post(
        "/api/v1/paper/order",
        json=market_order_body(order_type="LIMIT", price=1400.0),
    )

    assert response.status_code == 201
    assert response.json()["status"] == "OPEN"


def test_place_order_with_insufficient_cash_is_rejected_not_a_500() -> None:
    client = make_client(ltp=1_000_000.0, initial_capital=1000.0)

    response = client.post("/api/v1/paper/order", json=market_order_body(quantity=1))

    assert response.status_code == 201
    assert response.json()["status"] == "REJECTED"


def test_place_order_carries_metadata_through() -> None:
    client = make_client(ltp=1500.0)

    response = client.post(
        "/api/v1/paper/order",
        json={
            **market_order_body(),
            "metadata": {
                "confidence": 82.5,
                "agreeing_strategies": ["ema_trend", "rsi_macd_reversal"],
                "indicators_used": ["EMA", "RSI"],
                "reasoning": "BUY: 2 of 3 strategies agree.",
            },
        },
    )

    assert response.status_code == 201
    metadata = response.json()["metadata"]
    assert metadata["confidence"] == 82.5
    assert metadata["agreeing_strategies"] == ["ema_trend", "rsi_macd_reversal"]


def test_place_order_missing_required_field_returns_422() -> None:
    client = make_client()

    response = client.post(
        "/api/v1/paper/order",
        json={"order": {"exchange": "NSE", "side": "BUY", "order_type": "MARKET", "quantity": 10}},
    )

    assert response.status_code == 422


def test_place_order_broker_connection_failure_maps_to_503() -> None:
    app = create_app()
    broker = create_autospec(BrokerInterface, instance=True)
    broker.ltp.side_effect = BrokerConnectionError("network error", broker=None)
    app.dependency_overrides[get_broker] = lambda: broker
    app.dependency_overrides[get_paper_engine] = lambda: PaperTradingEngine()
    client = TestClient(app)

    response = client.post("/api/v1/paper/order", json=market_order_body())

    assert response.status_code == 503


# -- GET /api/v1/paper/orders ----------------------------------------------------------------


def test_get_orders_empty_initially() -> None:
    client = make_client()

    response = client.get("/api/v1/paper/orders")

    assert response.status_code == 200
    assert response.json() == []


def test_get_orders_lists_placed_orders() -> None:
    client = make_client(ltp=1500.0)
    client.post("/api/v1/paper/order", json=market_order_body())

    response = client.get("/api/v1/paper/orders")

    assert response.status_code == 200
    assert len(response.json()) == 1


# -- GET /api/v1/paper/positions -------------------------------------------------------------


def test_get_positions_reflects_an_open_order() -> None:
    client = make_client(ltp=1500.0)
    client.post("/api/v1/paper/order", json=market_order_body(quantity=10))

    response = client.get("/api/v1/paper/positions")

    assert response.status_code == 200
    positions = response.json()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "INFY-EQ"
    assert positions[0]["quantity"] == 10


# -- GET /api/v1/paper/portfolio -------------------------------------------------------------


def test_get_portfolio_reflects_cash_after_a_buy() -> None:
    client = make_client(ltp=1500.0)
    client.post("/api/v1/paper/order", json=market_order_body(quantity=10))

    response = client.get("/api/v1/paper/portfolio")

    assert response.status_code == 200
    portfolio = response.json()
    assert portfolio["cash"] == 100_000.0 - 15_000.0
    assert portfolio["used_capital"] == 15_000.0


def test_get_portfolio_before_any_trading() -> None:
    client = make_client()

    response = client.get("/api/v1/paper/portfolio")

    assert response.status_code == 200
    portfolio = response.json()
    assert portfolio["cash"] == 100_000.0
    assert portfolio["equity"] == 100_000.0
    assert portfolio["positions"] == []


# -- GET /api/v1/paper/trades ----------------------------------------------------------------


def test_get_trades_empty_initially() -> None:
    client = make_client()

    response = client.get("/api/v1/paper/trades")

    assert response.status_code == 200
    assert response.json() == []


def test_get_trades_reflects_a_closed_position() -> None:
    client = make_client(ltp=1500.0)
    client.post("/api/v1/paper/order", json=market_order_body(side="BUY", quantity=10))

    # Re-point the broker's ltp to a higher price for the closing leg.
    app = client.app  # type: ignore[attr-defined]
    broker = create_autospec(BrokerInterface, instance=True)
    broker.ltp.return_value = Quote(
        tradingsymbol="INFY-EQ", exchange=Exchange.NSE, last_price=1600.0
    )
    app.dependency_overrides[get_broker] = lambda: broker

    client.post("/api/v1/paper/order", json=market_order_body(side="SELL", quantity=10))

    response = client.get("/api/v1/paper/trades")

    assert response.status_code == 200
    trades = response.json()
    assert len(trades) == 1
    assert trades[0]["pnl"] == 1000.0


# -- POST /api/v1/paper/reset ----------------------------------------------------------------


def test_reset_restores_a_flat_portfolio() -> None:
    client = make_client(ltp=1500.0)
    client.post("/api/v1/paper/order", json=market_order_body())

    response = client.post("/api/v1/paper/reset", json={})

    assert response.status_code == 200
    portfolio = response.json()
    assert portfolio["cash"] == 100_000.0
    assert portfolio["positions"] == []
    assert client.get("/api/v1/paper/orders").json() == []
    assert client.get("/api/v1/paper/trades").json() == []


def test_reset_with_custom_initial_capital() -> None:
    client = make_client()

    response = client.post("/api/v1/paper/reset", json={"initial_capital": 250_000.0})

    assert response.status_code == 200
    assert response.json()["cash"] == 250_000.0


def test_reset_rejects_non_positive_initial_capital() -> None:
    client = make_client()

    response = client.post("/api/v1/paper/reset", json={"initial_capital": 0.0})

    assert response.status_code == 422
