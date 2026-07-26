"""Paper Trading Engine: `BrokerInterface` adapter.

`PaperBroker` implements the same `BrokerInterface` every real broker
adapter does, so `app.brokers.factory.get_broker_adapter` can hand one
back for `BrokerName.PAPER` and the rest of the application — anything
written against `BrokerInterface` (e.g. the Signal API, pointed at
`DEFAULT_BROKER=paper`) works unchanged, per this milestone's
Requirement 4.

Paper trading has no market data of its own: `ltp`/`historical_data`/
`start_websocket` all delegate to a real, wrapped `BrokerInterface`
(`market_data_broker`, chosen by `settings.paper_market_data_broker`).
Every price this class observes along the way — an `ltp()` snapshot, an
incoming WebSocket tick — is also fed into `PaperTradingEngine
.update_price`. That is how a resting paper `LIMIT`/`STOP` order ever
gets a chance to fill in this milestone (which deliberately has no
background scheduler — see `app.paper.engine`'s docstring): whatever
market-data activity is already happening through this adapter drives
the paper engine's matching for free, with no polling loop of its own.

`login`/`refresh_token`/`close` delegate to the wrapped broker (paper
trading needs no authentication of its own, but its market data does).
`place_order`/`modify_order`/`cancel_order`/`get_positions`/
`get_orders`/`get_funds` route entirely through `PaperTradingEngine` —
no real order ever reaches a real exchange through this class.
`get_holdings` returns an empty list: this milestone models only
intraday-style paper positions, no T+1/delivery concept.

`OrderNotFoundError`/`InvalidOrderStateError` (this package's own
exceptions — see `app.domain.exceptions.paper`) are translated to
`BrokerAPIError` at this class's boundary, so a caller written against
`BrokerInterface`'s documented `except BrokerError` contract catches
them here exactly as it would a real broker's rejection. The dedicated
`/api/v1/paper/*` REST endpoints call `PaperTradingEngine` directly
instead (see `app.api.v1.routers.paper`) and let those richer
paper-specific exceptions propagate unwrapped.
"""

from collections.abc import Sequence
from datetime import datetime

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
    OrderStatus,
    OrderType,
    OrderVariety,
    ProductType,
)
from app.domain.exceptions.broker import BrokerAPIError, OrderRejectionError
from app.domain.exceptions.paper import InvalidOrderStateError, OrderNotFoundError
from app.paper.dto import OrderModificationRequest, PaperOrderRequest, PaperOrderType
from app.paper.engine import PaperTradingEngine
from app.paper.models import PaperOrder, PaperPosition

#: Canonical `OrderType` (what `BrokerInterface.place_order` accepts) <->
#: this package's own, richer `PaperOrderType`. `TRAILING_STOP` has no
#: domain `OrderType` counterpart — it's only reachable via the
#: dedicated `/api/v1/paper/order` endpoint, never through this
#: generic `BrokerInterface` surface.
_ORDER_TYPE_TO_PAPER: dict[OrderType, PaperOrderType] = {
    OrderType.MARKET: PaperOrderType.MARKET,
    OrderType.LIMIT: PaperOrderType.LIMIT,
    OrderType.STOP_LOSS: PaperOrderType.STOP_LIMIT,
    OrderType.STOP_LOSS_MARKET: PaperOrderType.STOP,
}
_PAPER_TYPE_TO_ORDER_TYPE: dict[PaperOrderType, OrderType] = {
    PaperOrderType.MARKET: OrderType.MARKET,
    PaperOrderType.LIMIT: OrderType.LIMIT,
    PaperOrderType.STOP_LIMIT: OrderType.STOP_LOSS,
    PaperOrderType.STOP: OrderType.STOP_LOSS_MARKET,
    # No domain equivalent; STOP_LOSS_MARKET is the closest analog.
    PaperOrderType.TRAILING_STOP: OrderType.STOP_LOSS_MARKET,
}


class PaperBroker(BrokerInterface):
    """`BrokerInterface` implementation backed by `PaperTradingEngine`."""

    def __init__(
        self, market_data_broker: BrokerInterface, engine: PaperTradingEngine | None = None
    ) -> None:
        self._market_data_broker = market_data_broker
        self._engine = engine or PaperTradingEngine()

    @property
    def engine(self) -> PaperTradingEngine:
        """The underlying `PaperTradingEngine` — used directly by the
        dedicated `/api/v1/paper/*` REST endpoints, which need the
        richer surface (brackets, trade metadata, trade history)
        `BrokerInterface` itself has no room for.
        """

        return self._engine

    @property
    def market_data_broker(self) -> BrokerInterface:
        """The real `BrokerInterface` this instance sources prices from."""

        return self._market_data_broker

    # -- auth: delegates to the wrapped market-data broker -----------------------------------

    async def login(self) -> TokenBundle:
        return await self._market_data_broker.login()

    async def refresh_token(self) -> TokenBundle:
        return await self._market_data_broker.refresh_token()

    # -- account/profile: synthesized from the paper engine ------------------------------------

    async def get_profile(self) -> BrokerProfile:
        return BrokerProfile(
            broker=BrokerName.PAPER, client_id="PAPER", display_name="Paper Trading Account"
        )

    async def get_funds(self) -> BrokerFunds:
        portfolio = await self._engine.get_portfolio()
        return BrokerFunds(
            broker=BrokerName.PAPER,
            available_cash=portfolio.available_cash,
            used_margin=portfolio.used_capital,
            total_balance=portfolio.equity,
            raw={
                "cash": portfolio.cash,
                "realized_pnl": portfolio.realized_pnl,
                "unrealized_pnl": portfolio.unrealized_pnl,
                "total_pnl": portfolio.total_pnl,
            },
        )

    async def get_positions(self) -> list[Position]:
        positions = await self._engine.get_positions()
        return [_to_domain_position(position) for position in positions]

    async def get_holdings(self) -> list[Holding]:
        return []

    async def get_orders(self) -> list[OrderDetail]:
        orders = await self._engine.get_orders()
        return [_to_domain_order_detail(order) for order in orders]

    # -- orders: route through the paper engine -------------------------------------------------

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        current_price = (await self.ltp(order.exchange, order.tradingsymbol)).last_price
        request = _to_paper_request(order)

        placed = await self._engine.place_order(request, current_price=current_price)
        if placed.status is OrderStatus.REJECTED:
            raise OrderRejectionError(
                f"paper: order rejected: {placed.status_message}", broker=BrokerName.PAPER
            )
        return OrderResponse(
            order_id=placed.order_id, broker=BrokerName.PAPER, raw=_order_raw(placed)
        )

    async def modify_order(self, order_id: str, order: OrderRequest) -> OrderResponse:
        modification = OrderModificationRequest(
            order_id=order_id,
            quantity=order.quantity,
            price=order.price,
            trigger_price=order.trigger_price,
            validity=order.validity,
        )
        try:
            updated = await self._engine.modify_order(modification)
        except (OrderNotFoundError, InvalidOrderStateError) as exc:
            raise BrokerAPIError(str(exc), broker=BrokerName.PAPER) from exc
        return OrderResponse(
            order_id=updated.order_id, broker=BrokerName.PAPER, raw=_order_raw(updated)
        )

    async def cancel_order(
        self, order_id: str, variety: OrderVariety = OrderVariety.REGULAR
    ) -> OrderResponse:
        try:
            cancelled = await self._engine.cancel_order(order_id)
        except (OrderNotFoundError, InvalidOrderStateError) as exc:
            raise BrokerAPIError(str(exc), broker=BrokerName.PAPER) from exc
        return OrderResponse(
            order_id=cancelled.order_id, broker=BrokerName.PAPER, raw=_order_raw(cancelled)
        )

    # -- market data: delegate + feed the paper engine's matching --------------------------------

    async def ltp(self, exchange: Exchange, tradingsymbol: str) -> Quote:
        quote = await self._market_data_broker.ltp(exchange, tradingsymbol)
        await self._engine.update_price(tradingsymbol, exchange, quote.last_price)
        return quote

    async def historical_data(
        self,
        exchange: Exchange,
        tradingsymbol: str,
        interval: HistoricalInterval,
        from_date: datetime,
        to_date: datetime,
    ) -> list[HistoricalBar]:
        return await self._market_data_broker.historical_data(
            exchange, tradingsymbol, interval, from_date, to_date
        )

    async def start_websocket(
        self, on_tick: TickHandler, instruments: Sequence[tuple[Exchange, str]]
    ) -> None:
        async def relay(quote: Quote) -> None:
            await self._engine.update_price(quote.tradingsymbol, quote.exchange, quote.last_price)
            await on_tick(quote)

        await self._market_data_broker.start_websocket(relay, instruments)

    async def stop_websocket(self) -> None:
        await self._market_data_broker.stop_websocket()

    async def close(self) -> None:
        await self._market_data_broker.close()


def _to_paper_request(order: OrderRequest) -> PaperOrderRequest:
    return PaperOrderRequest(
        symbol=order.tradingsymbol,
        exchange=order.exchange,
        side=order.transaction_type,
        order_type=_ORDER_TYPE_TO_PAPER[order.order_type],
        quantity=order.quantity,
        price=order.price,
        trigger_price=order.trigger_price,
        validity=order.validity,
        tag=order.tag,
    )


def _order_raw(order: PaperOrder) -> dict[str, object]:
    return order.model_dump(mode="json")


def _to_domain_position(position: PaperPosition) -> Position:
    return Position(
        tradingsymbol=position.symbol,
        exchange=position.exchange,
        product=ProductType.INTRADAY,
        quantity=position.quantity,
        average_price=position.average_price,
        last_price=position.last_price,
        pnl=position.realized_pnl + position.unrealized_pnl,
    )


def _to_domain_order_detail(order: PaperOrder) -> OrderDetail:
    return OrderDetail(
        order_id=order.order_id,
        tradingsymbol=order.symbol,
        exchange=order.exchange,
        status=order.status,
        transaction_type=order.side,
        order_type=_PAPER_TYPE_TO_ORDER_TYPE[order.order_type],
        product=ProductType.INTRADAY,
        quantity=order.quantity,
        filled_quantity=order.filled_quantity,
        pending_quantity=order.quantity - order.filled_quantity,
        price=order.price or order.average_fill_price or 0.0,
        average_price=order.average_fill_price or 0.0,
        order_timestamp=order.updated_at,
        status_message=order.status_message,
    )
