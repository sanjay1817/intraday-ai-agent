"""Paper Trading Engine: order lifecycle and matching.

`OrderManager` owns every `app.paper.models.PaperOrder` and is the one
place order state actually changes — placement, resting-order matching
against a fresh price, cancellation, modification, bracket/OCO leg
spawning, and settlement (cash movement + position fills + trade-history
records) all happen here. It holds `PortfolioManager`/`PositionManager`
references (constructor-injected) because settlement inherently touches
both on every fill: reserving/releasing cash, debiting/crediting it,
applying the fill to the position, and recording whatever realized P&L
comes back.

Matching is deliberately request-driven, not clock-driven: there is no
background scheduler in this milestone (by design — see the project's
constraints), so a resting `LIMIT`/`STOP`/`STOP_LIMIT`/`TRAILING_STOP`
order only gets a chance to fill when something calls
`process_price_update` with a fresh price for its symbol. `app.paper.broker
.PaperBroker` wires this to already-happening activity (an `ltp()` call,
an incoming WebSocket tick) rather than polling on its own — see that
module's docstring.

A `MARKET` order always fills at placement, at whatever `current_price`
the caller supplies (this engine has no order book of its own — every
match is against one caller-supplied price point, at the market's
current level; there is no partial-fill/liquidity simulation).

Running out of paper money is a normal, expected outcome — not a system
failure — so `InsufficientCashError` during order settlement is caught
here and turned into a `REJECTED` order (mirroring how
`app.brokers.angel_one` turns a broker's own "insufficient funds"
response into `OrderRejectionError` rather than some generic crash). A
failed *modification* of an already-accepted order is different: it
raises instead, leaving the original order untouched, since silently
rejecting/destroying a previously valid order on a failed edit would
lose information a real caller needs to react to.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import structlog

from app.core.logging import TRADE_LOGGER_NAME
from app.domain.enums.trading import Exchange, OrderSide, OrderStatus
from app.domain.exceptions.paper import (
    InsufficientCashError,
    InvalidOrderStateError,
    OrderNotFoundError,
)
from app.paper.dto import OrderModificationRequest, PaperOrderRequest, PaperOrderType
from app.paper.models import ClosedTrade, PaperOrder, TradeMetadata
from app.paper.portfolio import PortfolioManager
from app.paper.position_manager import PositionManager

#: Order states from which a cancel/modify is meaningful.
_RESTING_STATUSES = frozenset({OrderStatus.OPEN, OrderStatus.TRIGGER_PENDING})

#: `PaperOrderType`s that never rest — they either fill or reject at
#: the moment of placement.
_IMMEDIATE_ORDER_TYPES = frozenset({PaperOrderType.MARKET})

#: `PaperOrderType`s that start life waiting for a trigger price to be
#: crossed, as opposed to `LIMIT`'s plain "waiting for a price".
_TRIGGER_ORDER_TYPES = frozenset(
    {PaperOrderType.STOP, PaperOrderType.STOP_LIMIT, PaperOrderType.TRAILING_STOP}
)

trade_logger = structlog.get_logger(TRADE_LOGGER_NAME)


class OrderManager:
    """Owns every simulated order and drives its lifecycle end to end."""

    def __init__(self, portfolio: PortfolioManager, positions: PositionManager) -> None:
        self._portfolio = portfolio
        self._positions = positions
        self._orders: dict[str, PaperOrder] = {}
        #: `order_id -> amount reserved` for a resting BUY order's
        #: worst-case cost. Not part of `PaperOrder` itself: it's this
        #: class's own bookkeeping for how much to hand back to
        #: `PortfolioManager` on fill/cancel/modify, not caller-visible
        #: order state.
        self._reservations: dict[str, float] = {}

    # -- placement ----------------------------------------------------------------------

    async def place_order(
        self,
        request: PaperOrderRequest,
        *,
        current_price: float,
        metadata: TradeMetadata | None = None,
        now: datetime | None = None,
    ) -> tuple[PaperOrder, list[ClosedTrade]]:
        """Place `request`, attempting to match it immediately against
        `current_price`. Returns the resulting order (`COMPLETE` if it
        filled outright, `OPEN`/`TRIGGER_PENDING` if it rests,
        `REJECTED` if it couldn't be funded) and any `ClosedTrade`s this
        placement produced (non-empty only if it immediately closed
        existing exposure).
        """

        if current_price <= 0:
            raise ValueError(f"current_price must be positive, got {current_price}")

        resolved_now = now or datetime.now(UTC)
        order_id = str(uuid4())

        trade_logger.info(
            "order_placement_requested",
            broker="paper",
            side=request.side.value,
            symbol=request.symbol,
            exchange=request.exchange.value,
            quantity=request.quantity,
            order_type=request.order_type.value,
            price=request.price,
            current_price=current_price,
            order_id=order_id,
        )

        trigger_price = request.trigger_price
        if request.order_type is PaperOrderType.TRAILING_STOP:
            trigger_price = self._initial_trailing_trigger(
                request.side, current_price, request.trailing_amount, request.trailing_percent
            )

        order = PaperOrder(
            order_id=order_id,
            symbol=request.symbol,
            exchange=request.exchange,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            price=request.price,
            trigger_price=trigger_price,
            trailing_amount=request.trailing_amount,
            trailing_percent=request.trailing_percent,
            status=self._initial_status(request.order_type),
            validity=request.validity,
            expires_at=request.expires_at,
            stop_loss_price=request.stop_loss_price,
            target_price=request.target_price,
            tag=request.tag,
            metadata=metadata,
            created_at=resolved_now,
            updated_at=resolved_now,
        )

        if order.side is OrderSide.BUY and order.order_type not in _IMMEDIATE_ORDER_TYPES:
            worst_case = order.price if order.price is not None else order.trigger_price
            assert worst_case is not None  # guaranteed by PaperOrderRequest's own validators
            amount = order.quantity * worst_case
            try:
                await self._portfolio.reserve_cash(amount)
            except InsufficientCashError as exc:
                trade_logger.warning(
                    "order_placement_rejected",
                    broker="paper",
                    side=order.side.value,
                    symbol=order.symbol,
                    order_id=order_id,
                    reason=str(exc),
                )
                rejected = order.model_copy(
                    update={"status": OrderStatus.REJECTED, "status_message": str(exc)}
                )
                self._orders[order_id] = rejected
                return rejected, []
            self._reservations[order_id] = amount

        self._orders[order_id] = order
        settled_order, closed_trades = await self._evaluate_and_settle(
            order, current_price, resolved_now
        )
        trade_logger.info(
            "order_placed",
            broker="paper",
            side=settled_order.side.value,
            symbol=settled_order.symbol,
            order_id=settled_order.order_id,
            status=settled_order.status.value,
        )
        return settled_order, closed_trades

    # -- cancel / modify ------------------------------------------------------------------

    async def cancel_order(self, order_id: str, *, now: datetime | None = None) -> PaperOrder:
        """Cancel a resting order, releasing whatever cash it had reserved.

        Raises:
            OrderNotFoundError: no order with this id exists.
            InvalidOrderStateError: the order isn't resting (already
                `COMPLETE`/`CANCELLED`/`REJECTED`).
        """

        order = self._require_resting_order(order_id)

        reserved = self._reservations.pop(order_id, 0.0)
        if reserved:
            await self._portfolio.release_cash(reserved)

        cancelled = order.model_copy(
            update={"status": OrderStatus.CANCELLED, "updated_at": now or datetime.now(UTC)}
        )
        self._orders[order_id] = cancelled
        return cancelled

    async def modify_order(
        self, modification: OrderModificationRequest, *, now: datetime | None = None
    ) -> PaperOrder:
        """Modify a resting order's quantity/price/trigger/validity/
        expiry. Only the fields set on `modification` change; the rest
        of the order (including its bracket/OCO linkage) is untouched.

        Re-reserves cash for the modified worst-case cost if this is a
        resting BUY order: on failure, the original reservation is
        restored and the order is left exactly as it was.

        Raises:
            OrderNotFoundError: no order with this id exists.
            InvalidOrderStateError: the order isn't resting.
            InsufficientCashError: the modified order can't be funded.
        """

        order = self._require_resting_order(modification.order_id)
        resolved_now = now or datetime.now(UTC)

        updated = order.model_copy(
            update={
                "quantity": modification.quantity or order.quantity,
                "price": modification.price if modification.price is not None else order.price,
                "trigger_price": (
                    modification.trigger_price
                    if modification.trigger_price is not None
                    else order.trigger_price
                ),
                "validity": modification.validity or order.validity,
                "expires_at": (
                    modification.expires_at
                    if modification.expires_at is not None
                    else order.expires_at
                ),
                "updated_at": resolved_now,
            }
        )

        if updated.side is OrderSide.BUY and updated.order_type not in _IMMEDIATE_ORDER_TYPES:
            old_reserved = self._reservations.get(order.order_id, 0.0)
            worst_case = updated.price if updated.price is not None else updated.trigger_price
            assert worst_case is not None
            new_reserved = updated.quantity * worst_case

            if old_reserved:
                await self._portfolio.release_cash(old_reserved)
            try:
                await self._portfolio.reserve_cash(new_reserved)
            except InsufficientCashError:
                if old_reserved:
                    await self._portfolio.reserve_cash(old_reserved)
                raise
            self._reservations[order.order_id] = new_reserved

        self._orders[order.order_id] = updated
        return updated

    def _require_resting_order(self, order_id: str) -> PaperOrder:
        order = self._orders.get(order_id)
        if order is None:
            raise OrderNotFoundError(order_id)
        if order.status not in _RESTING_STATUSES:
            raise InvalidOrderStateError(order_id, order.status.value)
        return order

    # -- price-driven matching --------------------------------------------------------------

    async def process_price_update(
        self, symbol: str, exchange: Exchange, price: float, *, now: datetime | None = None
    ) -> list[ClosedTrade]:
        """Re-evaluate every resting order for `symbol` against a fresh
        `price`, settling whatever now matches/triggers. Returns every
        `ClosedTrade` produced across all of them.
        """

        if price <= 0:
            raise ValueError(f"price must be positive, got {price}")

        resolved_now = now or datetime.now(UTC)
        # Snapshot candidate ids up front: settling one order (an OCO
        # cancel, a bracket spawn) mutates `self._orders` mid-loop.
        candidate_ids = [
            order_id
            for order_id, order in self._orders.items()
            if order.symbol == symbol
            and order.exchange is exchange
            and order.status in _RESTING_STATUSES
        ]

        closed_trades: list[ClosedTrade] = []
        for order_id in candidate_ids:
            order = self._orders.get(order_id)
            if order is None or order.status not in _RESTING_STATUSES:
                continue  # already settled as an OCO sibling earlier in this same loop
            _, new_trades = await self._evaluate_and_settle(order, price, resolved_now)
            closed_trades.extend(new_trades)
        return closed_trades

    # -- lookups / reset ----------------------------------------------------------------------

    async def get_order(self, order_id: str) -> PaperOrder | None:
        return self._orders.get(order_id)

    async def get_all_orders(self) -> list[PaperOrder]:
        return list(self._orders.values())

    async def reset(self) -> None:
        self._orders.clear()
        self._reservations.clear()

    # -- matching internals ---------------------------------------------------------------------

    @staticmethod
    def _initial_status(order_type: PaperOrderType) -> OrderStatus:
        if order_type in _TRIGGER_ORDER_TYPES:
            return OrderStatus.TRIGGER_PENDING
        return OrderStatus.OPEN

    @staticmethod
    def _initial_trailing_trigger(
        side: OrderSide,
        current_price: float,
        trailing_amount: float | None,
        trailing_percent: float | None,
    ) -> float:
        offset = (
            trailing_amount
            if trailing_amount is not None
            else current_price * (trailing_percent or 0.0) / 100.0
        )
        return current_price - offset if side is OrderSide.SELL else current_price + offset

    @staticmethod
    def _ratchet_trailing_trigger(order: PaperOrder, current_price: float) -> float:
        assert order.trigger_price is not None
        offset = (
            order.trailing_amount
            if order.trailing_amount is not None
            else current_price * (order.trailing_percent or 0.0) / 100.0
        )
        if order.side is OrderSide.SELL:
            # Protects a long: the trigger only ever ratchets upward as
            # price rises, never retreats if price pulls back.
            return max(order.trigger_price, current_price - offset)
        # Protects/covers a short: the trigger only ever ratchets downward.
        return min(order.trigger_price, current_price + offset)

    @staticmethod
    def _is_marketable(order: PaperOrder, price: float) -> bool:
        assert order.price is not None
        return price <= order.price if order.side is OrderSide.BUY else price >= order.price

    @staticmethod
    def _is_triggered(order: PaperOrder, price: float) -> bool:
        assert order.trigger_price is not None
        return (
            price >= order.trigger_price
            if order.side is OrderSide.BUY
            else price <= order.trigger_price
        )

    async def _evaluate_and_settle(
        self, order: PaperOrder, price: float, now: datetime
    ) -> tuple[PaperOrder, list[ClosedTrade]]:
        """Determine whether `order` fills against `price` right now
        (updating its resting state either way), and settle it if so.
        """

        working = order
        fill_price: float | None = None

        if working.order_type is PaperOrderType.MARKET:
            fill_price = price
        elif working.order_type is PaperOrderType.LIMIT:
            if self._is_marketable(working, price):
                fill_price = price
        elif working.order_type is PaperOrderType.STOP:
            if self._is_triggered(working, price):
                fill_price = price
        elif working.order_type is PaperOrderType.STOP_LIMIT:
            if working.status is OrderStatus.TRIGGER_PENDING:
                if self._is_triggered(working, price):
                    working = working.model_copy(
                        update={"status": OrderStatus.OPEN, "updated_at": now}
                    )
                    if self._is_marketable(working, price):
                        fill_price = price
            elif self._is_marketable(working, price):
                fill_price = price
        else:  # PaperOrderType.TRAILING_STOP
            new_trigger = self._ratchet_trailing_trigger(working, price)
            if new_trigger != working.trigger_price:
                working = working.model_copy(
                    update={"trigger_price": new_trigger, "updated_at": now}
                )
            if self._is_triggered(working, price):
                fill_price = price

        if fill_price is None:
            self._orders[working.order_id] = working
            return working, []

        return await self._settle_fill(working, fill_price, now)

    async def _settle_fill(
        self, order: PaperOrder, fill_price: float, now: datetime
    ) -> tuple[PaperOrder, list[ClosedTrade]]:
        reserved = self._reservations.pop(order.order_id, 0.0)
        if reserved:
            await self._portfolio.release_cash(reserved)

        cost = order.quantity * fill_price
        if order.side is OrderSide.BUY:
            try:
                await self._portfolio.debit(cost)
            except InsufficientCashError as exc:
                rejected = order.model_copy(
                    update={
                        "status": OrderStatus.REJECTED,
                        "status_message": str(exc),
                        "updated_at": now,
                    }
                )
                self._orders[order.order_id] = rejected
                return rejected, []
        else:
            await self._portfolio.credit(cost)

        fill_outcome = await self._positions.apply_fill(
            symbol=order.symbol,
            exchange=order.exchange,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            order_id=order.order_id,
            timestamp=now,
            metadata=order.metadata,
        )

        closed_trades: list[ClosedTrade] = []
        for closed_lot in fill_outcome.closed_lots:
            await self._portfolio.record_realized_pnl(closed_lot.realized_pnl)
            entry_order = self._orders.get(closed_lot.entry_order_id)
            closed_trades.append(
                ClosedTrade(
                    trade_id=str(uuid4()),
                    symbol=order.symbol,
                    exchange=order.exchange,
                    side=closed_lot.entry_side,
                    quantity=closed_lot.quantity,
                    entry_price=closed_lot.entry_price,
                    exit_price=closed_lot.exit_price,
                    stop_loss_price=entry_order.stop_loss_price if entry_order else None,
                    target_price=entry_order.target_price if entry_order else None,
                    pnl=closed_lot.realized_pnl,
                    entry_order_id=closed_lot.entry_order_id,
                    exit_order_id=order.order_id,
                    entry_timestamp=closed_lot.entry_timestamp,
                    exit_timestamp=closed_lot.exit_timestamp,
                    metadata=closed_lot.metadata,
                )
            )

        filled_order = order.model_copy(
            update={
                "status": OrderStatus.COMPLETE,
                "filled_quantity": order.quantity,
                "average_fill_price": fill_price,
                "updated_at": now,
            }
        )
        self._orders[order.order_id] = filled_order

        await self._cancel_oco_sibling(filled_order, now)
        await self._maybe_spawn_bracket_children(filled_order, now)

        # `_maybe_spawn_bracket_children` may have annotated the stored
        # order with a `status_message` (e.g. a skipped bracket leg)
        # after `filled_order` above was already built — re-read the
        # current stored value so the caller sees that too, instead of
        # a stale copy from before that update.
        return self._orders[order.order_id], closed_trades

    async def _cancel_oco_sibling(self, filled_order: PaperOrder, now: datetime) -> None:
        if filled_order.oco_group_id is None:
            return
        for other_id, other in list(self._orders.items()):
            if other_id == filled_order.order_id or other.oco_group_id != filled_order.oco_group_id:
                continue
            if other.status not in _RESTING_STATUSES:
                continue
            reserved = self._reservations.pop(other_id, 0.0)
            if reserved:
                await self._portfolio.release_cash(reserved)
            self._orders[other_id] = other.model_copy(
                update={
                    "status": OrderStatus.CANCELLED,
                    "status_message": "cancelled: OCO sibling filled",
                    "updated_at": now,
                }
            )

    async def _maybe_spawn_bracket_children(self, entry_order: PaperOrder, now: datetime) -> None:
        """Spawn the stop-loss/target exit legs an entry order's own
        `stop_loss_price`/`target_price` describe, once it fills.

        A leg whose price is no longer directionally consistent with
        the entry's *actual* fill price (only possible for a `MARKET`/
        `STOP` entry, whose fill price isn't known until match time —
        see `app.paper.dto.PaperOrderRequest`'s own docstring) is
        skipped rather than created nonsensically; the entry order's
        `status_message` records which leg(s) were skipped and why.
        """

        if entry_order.parent_order_id is not None:
            return  # this order is itself a bracket leg, not an entry
        if entry_order.stop_loss_price is None and entry_order.target_price is None:
            return

        fill_price = entry_order.average_fill_price
        assert fill_price is not None
        exit_side = OrderSide.SELL if entry_order.side is OrderSide.BUY else OrderSide.BUY
        group_id = str(uuid4())
        skipped: list[str] = []

        if entry_order.stop_loss_price is not None:
            if self._bracket_leg_is_valid(
                entry_order.side,
                is_stop_loss=True,
                leg_price=entry_order.stop_loss_price,
                fill_price=fill_price,
            ):
                await self._spawn_bracket_child(
                    entry_order,
                    exit_side,
                    PaperOrderType.STOP,
                    trigger_price=entry_order.stop_loss_price,
                    status=OrderStatus.TRIGGER_PENDING,
                    group_id=group_id,
                    now=now,
                )
            else:
                skipped.append(f"stop_loss_price {entry_order.stop_loss_price}")

        if entry_order.target_price is not None:
            if self._bracket_leg_is_valid(
                entry_order.side,
                is_stop_loss=False,
                leg_price=entry_order.target_price,
                fill_price=fill_price,
            ):
                await self._spawn_bracket_child(
                    entry_order,
                    exit_side,
                    PaperOrderType.LIMIT,
                    price=entry_order.target_price,
                    status=OrderStatus.OPEN,
                    group_id=group_id,
                    now=now,
                )
            else:
                skipped.append(f"target_price {entry_order.target_price}")

        if skipped:
            message = f"bracket leg(s) skipped (inconsistent with fill price {fill_price}): {'; '.join(skipped)}"
            self._orders[entry_order.order_id] = self._orders[entry_order.order_id].model_copy(
                update={"status_message": message}
            )

    @staticmethod
    def _bracket_leg_is_valid(
        entry_side: OrderSide, *, is_stop_loss: bool, leg_price: float, fill_price: float
    ) -> bool:
        if entry_side is OrderSide.BUY:
            return leg_price < fill_price if is_stop_loss else leg_price > fill_price
        return leg_price > fill_price if is_stop_loss else leg_price < fill_price

    async def _spawn_bracket_child(
        self,
        entry_order: PaperOrder,
        side: OrderSide,
        order_type: PaperOrderType,
        *,
        status: OrderStatus,
        group_id: str,
        now: datetime,
        price: float | None = None,
        trigger_price: float | None = None,
    ) -> None:
        child = PaperOrder(
            order_id=str(uuid4()),
            symbol=entry_order.symbol,
            exchange=entry_order.exchange,
            side=side,
            order_type=order_type,
            quantity=entry_order.quantity,
            price=price,
            trigger_price=trigger_price,
            status=status,
            validity=entry_order.validity,
            parent_order_id=entry_order.order_id,
            oco_group_id=group_id,
            tag=entry_order.tag,
            metadata=entry_order.metadata,
            created_at=now,
            updated_at=now,
        )

        if side is OrderSide.BUY:
            worst_case = price if price is not None else trigger_price
            assert worst_case is not None
            amount = child.quantity * worst_case
            try:
                await self._portfolio.reserve_cash(amount)
            except InsufficientCashError as exc:
                child = child.model_copy(
                    update={"status": OrderStatus.REJECTED, "status_message": str(exc)}
                )
            else:
                self._reservations[child.order_id] = amount

        self._orders[child.order_id] = child
