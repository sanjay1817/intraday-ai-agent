"""Unit tests for `app.paper.broker.PaperBroker`.

Uses a small in-memory fake `BrokerInterface` for market data — no real
network call, and full control over the price `PaperBroker` observes.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from app.brokers.base import BrokerInterface, TickHandler
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
    OrderType,
    OrderVariety,
    ProductType,
)
from app.domain.exceptions.broker import BrokerAPIError, OrderRejectionError
from app.paper.broker import PaperBroker
from app.paper.engine import PaperTradingEngine

_T0 = datetime(2024, 1, 1, 9, 15, tzinfo=UTC)


class _FakeMarketDataBroker(BrokerInterface):
    """Minimal in-memory `BrokerInterface` fake: fixed/queued LTPs, no I/O."""

    def __init__(self, ltp_by_symbol: dict[str, float] | None = None) -> None:
        self.ltp_by_symbol = ltp_by_symbol or {}
        self.login_called = False
        self.refresh_called = False
        self.closed = False
        self.stopped_ws = False
        self.ws_calls: list[tuple[TickHandler, Sequence[tuple[Exchange, str]]]] = []

    async def login(self) -> TokenBundle:
        self.login_called = True
        return TokenBundle(access_token="fake-token")

    async def refresh_token(self) -> TokenBundle:
        self.refresh_called = True
        return TokenBundle(access_token="fake-token-2")

    async def get_profile(self) -> BrokerProfile:
        return BrokerProfile(broker=BrokerName.ANGEL_ONE, client_id="X", display_name="X")

    async def get_funds(self) -> BrokerFunds:
        return BrokerFunds(
            broker=BrokerName.ANGEL_ONE, available_cash=0, used_margin=0, total_balance=0
        )

    async def get_positions(self) -> list[Position]:
        return []

    async def get_holdings(self) -> list[Holding]:
        return []

    async def get_orders(self) -> list[OrderDetail]:
        return []

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        raise AssertionError("PaperBroker must never place a real order")

    async def modify_order(self, order_id: str, order: OrderRequest) -> OrderResponse:
        raise AssertionError("PaperBroker must never modify a real order")

    async def cancel_order(
        self, order_id: str, variety: OrderVariety = OrderVariety.REGULAR
    ) -> OrderResponse:
        raise AssertionError("PaperBroker must never cancel a real order")

    async def ltp(self, exchange: Exchange, tradingsymbol: str) -> Quote:
        return Quote(
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            last_price=self.ltp_by_symbol[tradingsymbol],
            timestamp=_T0,
        )

    async def historical_data(
        self,
        exchange: Exchange,
        tradingsymbol: str,
        interval: HistoricalInterval,
        from_date: datetime,
        to_date: datetime,
    ) -> list[HistoricalBar]:
        return [
            HistoricalBar(timestamp=_T0, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000)
        ]

    async def start_websocket(
        self, on_tick: TickHandler, instruments: Sequence[tuple[Exchange, str]]
    ) -> None:
        self.ws_calls.append((on_tick, instruments))

    async def stop_websocket(self) -> None:
        self.stopped_ws = True

    async def close(self) -> None:
        self.closed = True


def make_broker(
    ltp_by_symbol: dict[str, float] | None = None
) -> tuple[PaperBroker, _FakeMarketDataBroker]:
    market_data = _FakeMarketDataBroker(ltp_by_symbol)
    return PaperBroker(market_data, engine=PaperTradingEngine()), market_data


def market_order(
    symbol: str = "INFY-EQ", side: OrderSide = OrderSide.BUY, quantity: int = 10
) -> OrderRequest:
    return OrderRequest(
        tradingsymbol=symbol,
        exchange=Exchange.NSE,
        transaction_type=side,
        quantity=quantity,
        order_type=OrderType.MARKET,
        product=ProductType.INTRADAY,
    )


# -- auth delegation --------------------------------------------------------------------


async def test_login_delegates_to_market_data_broker() -> None:
    broker, market_data = make_broker()

    await broker.login()

    assert market_data.login_called


async def test_refresh_token_delegates_to_market_data_broker() -> None:
    broker, market_data = make_broker()

    await broker.refresh_token()

    assert market_data.refresh_called


# -- profile / funds / positions / holdings / orders --------------------------------------


async def test_get_profile_is_synthesized() -> None:
    broker, _ = make_broker()

    profile = await broker.get_profile()

    assert profile.broker == BrokerName.PAPER
    assert profile.client_id == "PAPER"


async def test_get_funds_derives_from_engine_portfolio() -> None:
    broker, market_data = make_broker({"INFY-EQ": 1500.0})
    await broker.place_order(market_order())

    funds = await broker.get_funds()

    assert funds.broker == BrokerName.PAPER
    assert funds.total_balance == pytest.approx(
        100_000.0
    )  # equity unchanged by an open buy at cost
    assert funds.available_cash == pytest.approx(100_000.0 - 15_000.0)


async def test_get_holdings_is_always_empty() -> None:
    broker, _ = make_broker()

    assert await broker.get_holdings() == []


async def test_get_positions_translates_paper_positions() -> None:
    broker, _ = make_broker({"INFY-EQ": 1500.0})
    await broker.place_order(market_order())

    positions = await broker.get_positions()

    assert len(positions) == 1
    assert positions[0].tradingsymbol == "INFY-EQ"
    assert positions[0].quantity == 10
    assert positions[0].product == ProductType.INTRADAY


async def test_get_orders_translates_paper_orders() -> None:
    broker, _ = make_broker({"INFY-EQ": 1500.0})
    await broker.place_order(market_order())

    orders = await broker.get_orders()

    assert len(orders) == 1
    assert orders[0].tradingsymbol == "INFY-EQ"
    assert orders[0].order_type == OrderType.MARKET


# -- place_order --------------------------------------------------------------------------


async def test_place_order_fetches_ltp_and_places_through_the_engine() -> None:
    broker, _ = make_broker({"INFY-EQ": 1500.0})

    response = await broker.place_order(market_order())

    assert response.broker == BrokerName.PAPER
    assert response.order_id
    positions = await broker.engine.get_positions()
    assert positions[0].quantity == 10


async def test_place_order_insufficient_cash_raises_order_rejection_error() -> None:
    broker, _ = make_broker({"INFY-EQ": 1_000_000.0})

    with pytest.raises(OrderRejectionError):
        await broker.place_order(market_order(quantity=1))


# -- modify / cancel: exception translation --------------------------------------------------


async def test_modify_order_delegates_through_engine() -> None:
    broker, _ = make_broker({"INFY-EQ": 1500.0})
    placed = await broker.place_order(
        OrderRequest(
            tradingsymbol="INFY-EQ",
            exchange=Exchange.NSE,
            transaction_type=OrderSide.BUY,
            quantity=10,
            order_type=OrderType.LIMIT,
            product=ProductType.INTRADAY,
            price=1400.0,
        )
    )

    response = await broker.modify_order(
        placed.order_id,
        OrderRequest(
            tradingsymbol="INFY-EQ",
            exchange=Exchange.NSE,
            transaction_type=OrderSide.BUY,
            quantity=20,
            order_type=OrderType.LIMIT,
            product=ProductType.INTRADAY,
            price=1400.0,
        ),
    )

    updated = await broker.engine.get_order(placed.order_id)
    assert updated is not None
    assert updated.quantity == 20
    assert response.order_id == placed.order_id


async def test_modify_unknown_order_raises_broker_api_error() -> None:
    broker, _ = make_broker()

    with pytest.raises(BrokerAPIError):
        await broker.modify_order("no-such-id", market_order())


async def test_cancel_order_delegates_through_engine() -> None:
    broker, _ = make_broker({"INFY-EQ": 1500.0})
    placed = await broker.place_order(
        OrderRequest(
            tradingsymbol="INFY-EQ",
            exchange=Exchange.NSE,
            transaction_type=OrderSide.BUY,
            quantity=10,
            order_type=OrderType.LIMIT,
            product=ProductType.INTRADAY,
            price=1400.0,
        )
    )

    await broker.cancel_order(placed.order_id)

    cancelled = await broker.engine.get_order(placed.order_id)
    assert cancelled is not None
    assert cancelled.status.value == "CANCELLED"


async def test_cancel_unknown_order_raises_broker_api_error() -> None:
    broker, _ = make_broker()

    with pytest.raises(BrokerAPIError):
        await broker.cancel_order("no-such-id")


async def test_cancel_already_complete_order_raises_broker_api_error() -> None:
    broker, _ = make_broker({"INFY-EQ": 1500.0})
    placed = await broker.place_order(market_order())

    with pytest.raises(BrokerAPIError):
        await broker.cancel_order(placed.order_id)


# -- market data: delegation + price feed to the engine ---------------------------------------


async def test_ltp_delegates_and_feeds_the_engine() -> None:
    broker, _ = make_broker({"INFY-EQ": 1500.0})
    resting = await broker.place_order(
        OrderRequest(
            tradingsymbol="INFY-EQ",
            exchange=Exchange.NSE,
            transaction_type=OrderSide.BUY,
            quantity=10,
            order_type=OrderType.LIMIT,
            product=ProductType.INTRADAY,
            price=1600.0,
        )
    )
    # placing at 1500 with a limit of 1600 fills immediately (marketable);
    # use a non-marketable limit instead to actually leave it resting.
    resting_order = await broker.engine.get_order(resting.order_id)
    assert resting_order is not None

    broker2, market_data2 = make_broker({"INFY-EQ": 1500.0})
    placed = await broker2.place_order(
        OrderRequest(
            tradingsymbol="INFY-EQ",
            exchange=Exchange.NSE,
            transaction_type=OrderSide.BUY,
            quantity=10,
            order_type=OrderType.LIMIT,
            product=ProductType.INTRADAY,
            price=1400.0,  # not marketable at 1500 -> rests OPEN
        )
    )
    open_order = await broker2.engine.get_order(placed.order_id)
    assert open_order is not None
    assert open_order.status.value == "OPEN"

    market_data2.ltp_by_symbol["INFY-EQ"] = 1390.0
    quote = await broker2.ltp(Exchange.NSE, "INFY-EQ")

    assert quote.last_price == 1390.0
    filled = await broker2.engine.get_order(placed.order_id)
    assert filled is not None
    assert filled.status.value == "COMPLETE"


async def test_historical_data_delegates_without_touching_the_engine() -> None:
    broker, _ = make_broker()

    bars = await broker.historical_data(
        Exchange.NSE, "INFY-EQ", HistoricalInterval.FIVE_MINUTE, _T0, _T0
    )

    assert len(bars) == 1
    assert await broker.engine.get_positions() == []


async def test_start_websocket_wraps_on_tick_to_also_feed_the_engine() -> None:
    broker, market_data = make_broker({"INFY-EQ": 1500.0})
    placed = await broker.place_order(
        OrderRequest(
            tradingsymbol="INFY-EQ",
            exchange=Exchange.NSE,
            transaction_type=OrderSide.BUY,
            quantity=10,
            order_type=OrderType.LIMIT,
            product=ProductType.INTRADAY,
            price=1400.0,
        )
    )
    open_order = await broker.engine.get_order(placed.order_id)
    assert open_order is not None
    assert open_order.status.value == "OPEN"

    received: list[Quote] = []

    async def on_tick(quote: Quote) -> None:
        received.append(quote)

    await broker.start_websocket(on_tick, [(Exchange.NSE, "INFY-EQ")])
    assert len(market_data.ws_calls) == 1
    wrapped_on_tick, instruments = market_data.ws_calls[0]
    assert instruments == [(Exchange.NSE, "INFY-EQ")]

    await wrapped_on_tick(
        Quote(tradingsymbol="INFY-EQ", exchange=Exchange.NSE, last_price=1390.0, timestamp=_T0)
    )

    assert len(received) == 1  # the caller's own on_tick still fires
    filled = await broker.engine.get_order(placed.order_id)
    assert filled is not None
    assert filled.status.value == "COMPLETE"  # and the engine matched it too


async def test_stop_websocket_and_close_delegate() -> None:
    broker, market_data = make_broker()

    await broker.stop_websocket()
    await broker.close()

    assert market_data.stopped_ws
    assert market_data.closed
