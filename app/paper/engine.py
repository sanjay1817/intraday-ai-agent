"""Paper Trading Engine: the public facade.

`PaperTradingEngine` wires `PortfolioManager` + `PositionManager` +
`OrderManager` together and is the one object anything outside this
package (the REST API router, `app.paper.broker.PaperBroker`) ever talks
to — none of them import the three manager classes directly. It also
owns the one piece of state none of those three manages on its own: the
running, append-only trade history (`get_trades`), since
`OrderManager.place_order`/`process_price_update` only ever *return* the
`ClosedTrade`s a single call produced, not accumulate them.

`update_price` is this engine's hook for keeping resting orders and
open positions honest without a background scheduler (this milestone
deliberately has none): it re-evaluates every resting order for
`symbol` against the given price (which may fill some of them) and then
marks any still-open position to that same price for MTM — so calling
it from wherever a fresh price is naturally already being fetched (see
`PaperBroker`) is enough to keep the whole engine current, with no
polling loop of its own.
"""

from datetime import UTC, datetime

from app.domain.enums.trading import Exchange
from app.paper.dto import OrderModificationRequest, PaperOrderRequest
from app.paper.models import ClosedTrade, PaperOrder, PaperPosition, Portfolio, TradeMetadata
from app.paper.order_manager import OrderManager
from app.paper.portfolio import PortfolioManager
from app.paper.position_manager import PositionManager

#: The paper account's starting cash absent an explicit override —
#: matches the round-number default this package's own tests already
#: use throughout.
_DEFAULT_INITIAL_CAPITAL = 100_000.0


class PaperTradingEngine:
    """The Paper Trading Engine's single public entry point."""

    def __init__(self, initial_capital: float = _DEFAULT_INITIAL_CAPITAL) -> None:
        self._portfolio = PortfolioManager(initial_capital=initial_capital)
        self._positions = PositionManager()
        self._orders = OrderManager(self._portfolio, self._positions)
        self._trade_history: list[ClosedTrade] = []

    # -- orders -----------------------------------------------------------------------

    async def place_order(
        self,
        request: PaperOrderRequest,
        *,
        current_price: float,
        metadata: TradeMetadata | None = None,
        now: datetime | None = None,
    ) -> PaperOrder:
        """Place `request`, matching it immediately against `current_price`.

        See `OrderManager.place_order` for the full matching/settlement
        contract (immediate fill vs. resting vs. rejection).
        """

        order, trades = await self._orders.place_order(
            request, current_price=current_price, metadata=metadata, now=now
        )
        self._trade_history.extend(trades)
        return order

    async def cancel_order(self, order_id: str, *, now: datetime | None = None) -> PaperOrder:
        return await self._orders.cancel_order(order_id, now=now)

    async def modify_order(
        self, modification: OrderModificationRequest, *, now: datetime | None = None
    ) -> PaperOrder:
        return await self._orders.modify_order(modification, now=now)

    async def get_order(self, order_id: str) -> PaperOrder | None:
        return await self._orders.get_order(order_id)

    async def get_orders(self) -> list[PaperOrder]:
        return await self._orders.get_all_orders()

    # -- price feed ---------------------------------------------------------------------

    async def update_price(
        self, symbol: str, exchange: Exchange, price: float, *, now: datetime | None = None
    ) -> list[ClosedTrade]:
        """Feed a fresh price for `symbol` into the engine: resting
        orders get a chance to fill/trigger, and any open position is
        marked to `price` for MTM. Returns whatever `ClosedTrade`s this
        produced (empty if nothing matched).
        """

        resolved_now = now or datetime.now(UTC)
        trades = await self._orders.process_price_update(symbol, exchange, price, now=resolved_now)
        self._trade_history.extend(trades)
        await self._positions.update_last_price(symbol, exchange, price, now=resolved_now)
        return trades

    # -- positions / portfolio / trades ----------------------------------------------------

    async def get_position(self, symbol: str, exchange: Exchange) -> PaperPosition | None:
        return await self._positions.get_position(symbol, exchange)

    async def get_positions(self) -> list[PaperPosition]:
        return await self._positions.get_all_positions()

    async def get_portfolio(self, *, now: datetime | None = None) -> Portfolio:
        positions = await self._positions.get_all_positions()
        return await self._portfolio.snapshot(positions, now=now)

    async def get_trades(self) -> list[ClosedTrade]:
        return list(self._trade_history)

    # -- reset ------------------------------------------------------------------------------

    async def reset(self, initial_capital: float | None = None) -> None:
        """Reset every part of the engine to a fresh starting state —
        backs `POST /api/v1/paper/reset`.
        """

        await self._portfolio.reset(initial_capital)
        await self._positions.reset()
        await self._orders.reset()
        self._trade_history.clear()
