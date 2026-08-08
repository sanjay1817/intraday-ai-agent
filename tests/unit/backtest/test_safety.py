"""Safety-boundary tests: a historical backtest must never place a real
order through the injected broker — only `historical_data()` may ever be
called on it. Mirrors `tests/unit/auto/test_orchestrator.py::_FakeBroker`'s
own convention: every unused `BrokerInterface` method raises if called.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.backtest.dto import BacktestRequest
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

_FROM = datetime(2026, 8, 5, 9, 15, tzinfo=UTC)


class _NoOrdersAllowedBroker(BrokerInterface):
    """Only `historical_data()` is implemented — every order-placement
    (or otherwise live-broker) method raises `AssertionError` if ever
    called, proving a backtest run never reaches it.
    """

    def __init__(self, bars: list[HistoricalBar]) -> None:
        self._bars = bars

    async def login(self) -> TokenBundle:
        raise AssertionError("a historical backtest must never log in to a live broker")

    async def refresh_token(self) -> TokenBundle:
        raise AssertionError("not used by a backtest")

    async def get_profile(self) -> BrokerProfile:
        raise AssertionError("not used by a backtest")

    async def get_funds(self) -> BrokerFunds:
        raise AssertionError("not used by a backtest")

    async def get_positions(self) -> list[Position]:
        raise AssertionError("not used by a backtest")

    async def get_holdings(self) -> list[Holding]:
        raise AssertionError("not used by a backtest")

    async def get_orders(self) -> list[OrderDetail]:
        raise AssertionError("not used by a backtest")

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        raise AssertionError("HISTORICAL BACKTEST MUST NEVER PLACE A REAL ORDER")

    async def modify_order(self, order_id: str, order: OrderRequest) -> OrderResponse:
        raise AssertionError("HISTORICAL BACKTEST MUST NEVER MODIFY A REAL ORDER")

    async def cancel_order(
        self, order_id: str, variety: OrderVariety = OrderVariety.REGULAR
    ) -> OrderResponse:
        raise AssertionError("HISTORICAL BACKTEST MUST NEVER CANCEL A REAL ORDER")

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
        raise AssertionError("not used by a backtest")

    async def stop_websocket(self) -> None:
        raise AssertionError("not used by a backtest")

    async def close(self) -> None:
        raise AssertionError("not used by a backtest")


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


@pytest.mark.asyncio
async def test_run_single_never_calls_any_order_placement_method(tmp_path: Path) -> None:
    broker = _NoOrdersAllowedBroker(_trending_bars(60))
    orchestrator = BacktestOrchestrator(broker, BrokerName.ANGEL_ONE, data_dir=tmp_path)  # type: ignore[arg-type]

    request = BacktestRequest(
        symbol="RELIANCE-EQ",
        exchange=Exchange.NSE,
        historical_date=_FROM.date(),
        initial_capital=50_000.0,
        interval=HistoricalInterval.ONE_MINUTE,
    )

    # `_NoOrdersAllowedBroker.place_order`/`modify_order`/`cancel_order`/
    # `ltp` all raise `AssertionError` if the orchestrator or its replay
    # engine ever reach them -- a passing run proves they never were.
    result = await orchestrator.run_single(request, today=_FROM.date() + timedelta(days=1))

    assert result.disclaimer.startswith("HISTORICAL BACKTEST")
    assert "NO REAL ORDERS PLACED" in result.disclaimer
