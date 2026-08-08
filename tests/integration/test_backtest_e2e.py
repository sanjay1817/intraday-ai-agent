"""End-to-end test: Historical Backtesting driven entirely through the
real REST API surface (`POST /api/v1/backtest/run`), mirroring
`tests/integration/test_auto_trading_e2e.py`'s own dependency-override
pattern.

A fake `BrokerInterface` implementing only `historical_data()` stands in
for Angel One — every other method raises `AssertionError`, proving the
real request never reaches a live order-placement/authentication call.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.v1.routers.backtest import get_backtest_orchestrator
from app.backtest.session import BacktestOrchestrator
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
from app.domain.enums.trading import BrokerName, Exchange, HistoricalInterval, OrderVariety
from app.main import create_app

_FROM = datetime(2026, 8, 5, 9, 15, tzinfo=UTC)


class _FakeBroker(BrokerInterface):
    def __init__(self, bars: list[HistoricalBar]) -> None:
        self._bars = bars

    async def login(self) -> TokenBundle:
        raise AssertionError("a historical backtest must never log in to a live broker")

    async def refresh_token(self) -> TokenBundle:
        raise AssertionError

    async def get_profile(self) -> BrokerProfile:
        raise AssertionError

    async def get_funds(self) -> BrokerFunds:
        raise AssertionError

    async def get_positions(self) -> list[Position]:
        raise AssertionError

    async def get_holdings(self) -> list[Holding]:
        raise AssertionError

    async def get_orders(self) -> list[OrderDetail]:
        raise AssertionError

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        raise AssertionError("HISTORICAL BACKTEST MUST NEVER PLACE A REAL ORDER")

    async def modify_order(self, order_id: str, order: OrderRequest) -> OrderResponse:
        raise AssertionError

    async def cancel_order(
        self, order_id: str, variety: OrderVariety = OrderVariety.REGULAR
    ) -> OrderResponse:
        raise AssertionError

    async def ltp(self, exchange: Exchange, tradingsymbol: str) -> Quote:
        raise AssertionError("a backtest prices fills from historical candles, not live LTP")

    async def historical_data(
        self,
        exchange: Exchange,
        tradingsymbol: str,
        interval: HistoricalInterval,
        from_date: datetime,
        to_date: datetime,
    ) -> list[HistoricalBar]:
        return self._bars

    async def start_websocket(self, *args: object, **kwargs: object) -> None:
        raise AssertionError

    async def stop_websocket(self) -> None:
        raise AssertionError

    async def close(self) -> None:
        raise AssertionError


def _trending_bars(n: int) -> list[HistoricalBar]:
    price = 100.0
    bars = []
    for i in range(n):
        open_ = price
        price = open_ * 1.004
        bars.append(
            HistoricalBar(
                timestamp=_FROM + timedelta(minutes=i),
                open=open_,
                high=price * 1.001,
                low=open_ * 0.999,
                close=price,
                volume=1_000,
            )
        )
    return bars


def test_run_backtest_full_lifecycle_through_the_rest_api(tmp_path: Path) -> None:
    app = create_app()
    broker = _FakeBroker(_trending_bars(60))
    orchestrator = BacktestOrchestrator(broker, BrokerName.ANGEL_ONE, data_dir=tmp_path)
    app.dependency_overrides[get_backtest_orchestrator] = lambda: orchestrator

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/backtest/run",
            json={
                "symbol": "RELIANCE-EQ",
                "exchange": "NSE",
                "historical_date": "2026-08-05",
                "start_time": "09:15:00",
                "end_time": "10:15:00",
                "interval": "1minute",
                "initial_capital": 50_000.0,
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["disclaimer"].startswith("HISTORICAL BACKTEST")
        assert "NO REAL ORDERS PLACED" in body["disclaimer"]
        assert body["summary"]["initial_capital"] == 50_000.0
        assert isinstance(body["trades"], list)
        assert isinstance(body["equity_curve"], list)
        assert isinstance(body["signal_log"], list)
        assert len(body["signal_log"]) == 60

        run_id = body["run_id"]

        # -- the run is listed and reloadable through the read endpoints --
        list_response = client.get("/api/v1/backtest/runs")
        assert run_id in list_response.json()

        get_response = client.get(f"/api/v1/backtest/runs/{run_id}")
        assert get_response.status_code == 200
        assert get_response.json()["run_id"] == run_id

        trades_response = client.get(f"/api/v1/backtest/runs/{run_id}/trades")
        assert trades_response.status_code == 200

        signals_response = client.get(f"/api/v1/backtest/runs/{run_id}/signals")
        assert signals_response.status_code == 200
        assert len(signals_response.json()) == 60


def test_run_backtest_rejects_a_future_date(tmp_path: Path) -> None:
    app = create_app()
    broker = _FakeBroker(_trending_bars(60))
    orchestrator = BacktestOrchestrator(broker, BrokerName.ANGEL_ONE, data_dir=tmp_path)
    app.dependency_overrides[get_backtest_orchestrator] = lambda: orchestrator

    with TestClient(app) as client:
        far_future = (datetime.now(UTC) + timedelta(days=3650)).date().isoformat()
        response = client.post(
            "/api/v1/backtest/run",
            json={
                "symbol": "RELIANCE-EQ",
                "exchange": "NSE",
                "historical_date": far_future,
                "initial_capital": 50_000.0,
            },
        )

        assert response.status_code == 400
        assert response.json()["error"]["type"] == "FutureDateError"


def test_unknown_run_id_returns_404(tmp_path: Path) -> None:
    app = create_app()
    broker = _FakeBroker([])
    orchestrator = BacktestOrchestrator(broker, BrokerName.ANGEL_ONE, data_dir=tmp_path)
    app.dependency_overrides[get_backtest_orchestrator] = lambda: orchestrator

    with TestClient(app) as client:
        response = client.get("/api/v1/backtest/runs/does-not-exist")
        assert response.status_code == 404
