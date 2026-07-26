"""Paper Trading Engine: position tracking.

`PositionManager` owns every open `PaperPosition`, keyed by
`(Exchange, symbol)`. Internally it tracks each position as a FIFO queue
of entry lots (`_Lot`) — not just a single running average — because
`app.paper.models.ClosedTrade` (Requirement 5) needs a specific entry
order/timestamp/price per closed trade, and a single weighted average
can't answer "which entry did this exit actually close". The
externally-visible `PaperPosition.average_price` is still the
weighted-average cost across whatever lots remain open, matching that
model's own documented semantics — the FIFO queue is purely an internal
bookkeeping detail of this class, never exposed directly.

A fill that trades opposite to a position's current lots closes them
oldest-first (FIFO); a fill larger than the remaining open quantity
"flips" through flat into a new position on the other side, closing
every existing lot and opening a fresh one — the same fill can therefore
produce several `ClosedLot`s (one per entry lot it closes across) plus,
on a flip, a brand new position.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import UTC, datetime

from app.domain.enums.trading import Exchange, OrderSide
from app.paper.models import PaperPosition, TradeMetadata

_PositionKey = tuple[Exchange, str]


@dataclasses.dataclass(frozen=True, slots=True)
class _Lot:
    """One still-open entry fill within a position's FIFO queue.

    `quantity` is always a positive magnitude; `side` (shared by every
    lot currently in the same key's queue — see the module docstring)
    is what gives the queue its direction.
    """

    order_id: str
    timestamp: datetime
    price: float
    quantity: int
    side: OrderSide
    metadata: TradeMetadata | None


@dataclasses.dataclass(frozen=True, slots=True)
class ClosedLot:
    """One entry lot fully or partially closed by a single fill.

    The building block `app.paper.order_manager` turns into
    `app.paper.models.ClosedTrade` records — one `ClosedTrade` per
    `ClosedLot`, since each carries its own distinct entry order/price/
    timestamp even when several are produced by the same closing fill.
    """

    entry_order_id: str
    entry_timestamp: datetime
    entry_price: float
    entry_side: OrderSide
    quantity: int
    exit_price: float
    exit_timestamp: datetime
    realized_pnl: float
    metadata: TradeMetadata | None


@dataclasses.dataclass(frozen=True, slots=True)
class FillOutcome:
    """What applying one fill did to a position."""

    position: PaperPosition | None
    closed_lots: list[ClosedLot]
    realized_pnl_delta: float


class PositionManager:
    """Owns every open `PaperPosition` and the FIFO lot bookkeeping
    behind it.

    Every public method is `async def` for the same reason
    `app.paper.portfolio.PortfolioManager`'s are — see that module's
    docstring: today's implementation is pure in-memory arithmetic, but
    the public API is designed to survive becoming database-backed later
    without changing shape.
    """

    def __init__(self) -> None:
        self._lots: dict[_PositionKey, list[_Lot]] = {}
        self._positions: dict[_PositionKey, PaperPosition] = {}
        self._realized_pnl_since_open: dict[_PositionKey, float] = {}

    async def apply_fill(
        self,
        *,
        symbol: str,
        exchange: Exchange,
        side: OrderSide,
        quantity: int,
        price: float,
        order_id: str,
        timestamp: datetime,
        metadata: TradeMetadata | None = None,
    ) -> FillOutcome:
        """Apply one fill to `symbol`'s position, closing existing
        opposite-side lots FIFO first and opening/extending a lot with
        whatever quantity remains.

        Raises:
            ValueError: `quantity` or `price` is not positive.
        """

        if quantity <= 0:
            raise ValueError(f"quantity must be positive, got {quantity}")
        if price <= 0:
            raise ValueError(f"price must be positive, got {price}")

        key = (exchange, symbol)
        lots = self._lots.setdefault(key, [])
        previous_side = lots[0].side if lots else None

        remaining = quantity
        closed_lots: list[ClosedLot] = []

        if previous_side is not None and previous_side is not side:
            remaining, closed_lots = self._close_lots_fifo(
                lots, side=side, remaining=remaining, price=price, timestamp=timestamp
            )

        # True exactly when this fill starts a brand new position
        # lifecycle: either there was nothing open before, or closing
        # fully drained what was open (an exact close, or the first half
        # of a flip). A fresh lifecycle's own realized P&L starts at 0 —
        # `closed_lots`/`realized_pnl_delta` (returned below) still
        # report whatever the *old* lifecycle realized, for the caller
        # to book against the account-wide ledger and trade history.
        starts_fresh_lifecycle = previous_side is None or not lots

        if remaining > 0:
            lots.append(
                _Lot(
                    order_id=order_id,
                    timestamp=timestamp,
                    price=price,
                    quantity=remaining,
                    side=side,
                    metadata=metadata,
                )
            )

        fill_realized = sum(lot.realized_pnl for lot in closed_lots)
        if starts_fresh_lifecycle:
            self._realized_pnl_since_open[key] = 0.0
        else:
            self._realized_pnl_since_open[key] = (
                self._realized_pnl_since_open.get(key, 0.0) + fill_realized
            )

        position = self._rebuild_position(key, lots, mark_price=price, now=timestamp)

        return FillOutcome(
            position=position, closed_lots=closed_lots, realized_pnl_delta=fill_realized
        )

    @staticmethod
    def _close_lots_fifo(
        lots: list[_Lot], *, side: OrderSide, remaining: int, price: float, timestamp: datetime
    ) -> tuple[int, list[ClosedLot]]:
        """Consume `lots` oldest-first against an opposite-side fill of
        `remaining` quantity, mutating `lots` in place. Returns whatever
        quantity is still unconsumed (0 unless the fill outsizes every
        existing lot) and the `ClosedLot` produced per lot touched.
        """

        closed_lots: list[ClosedLot] = []
        while remaining > 0 and lots:
            lot = lots[0]
            closed_quantity = min(lot.quantity, remaining)
            if lot.side is OrderSide.BUY:
                realized_pnl = (price - lot.price) * closed_quantity
            else:
                realized_pnl = (lot.price - price) * closed_quantity

            closed_lots.append(
                ClosedLot(
                    entry_order_id=lot.order_id,
                    entry_timestamp=lot.timestamp,
                    entry_price=lot.price,
                    entry_side=lot.side,
                    quantity=closed_quantity,
                    exit_price=price,
                    exit_timestamp=timestamp,
                    realized_pnl=realized_pnl,
                    metadata=lot.metadata,
                )
            )

            remaining -= closed_quantity
            if closed_quantity == lot.quantity:
                lots.pop(0)
            else:
                lots[0] = dataclasses.replace(lot, quantity=lot.quantity - closed_quantity)

        return remaining, closed_lots

    def _rebuild_position(
        self, key: _PositionKey, lots: list[_Lot], *, mark_price: float, now: datetime
    ) -> PaperPosition | None:
        """Recompute `key`'s `PaperPosition` from its current lot queue,
        storing (or removing, if now flat) it in `self._positions`.
        """

        if not lots:
            self._lots.pop(key, None)
            self._realized_pnl_since_open.pop(key, None)
            self._positions.pop(key, None)
            return None

        exchange, symbol = key
        net_quantity = sum(lot.quantity for lot in lots)
        signed_quantity = net_quantity if lots[0].side is OrderSide.BUY else -net_quantity
        average_price = sum(lot.quantity * lot.price for lot in lots) / net_quantity
        unrealized_pnl = (mark_price - average_price) * signed_quantity
        opened_at = min(lot.timestamp for lot in lots)

        position = PaperPosition(
            symbol=symbol,
            exchange=exchange,
            quantity=signed_quantity,
            average_price=average_price,
            last_price=mark_price,
            realized_pnl=self._realized_pnl_since_open.get(key, 0.0),
            unrealized_pnl=unrealized_pnl,
            opened_at=opened_at,
            updated_at=now,
        )
        self._positions[key] = position
        return position

    async def update_last_price(
        self, symbol: str, exchange: Exchange, price: float, *, now: datetime | None = None
    ) -> PaperPosition | None:
        """Mark `symbol`'s open position to a new price with no fill —
        recomputes `unrealized_pnl`/`last_price` only. A no-op (returns
        `None`) if no position is currently open for `symbol`.

        Raises:
            ValueError: `price` is not positive.
        """

        if price <= 0:
            raise ValueError(f"price must be positive, got {price}")

        key = (exchange, symbol)
        lots = self._lots.get(key)
        if not lots:
            return None

        return self._rebuild_position(key, lots, mark_price=price, now=now or datetime.now(UTC))

    async def get_position(self, symbol: str, exchange: Exchange) -> PaperPosition | None:
        return self._positions.get((exchange, symbol))

    async def get_all_positions(self) -> list[PaperPosition]:
        return list(self._positions.values())

    async def reset(self) -> None:
        self._lots.clear()
        self._positions.clear()
        self._realized_pnl_since_open.clear()


__all__: Sequence[str] = ("ClosedLot", "FillOutcome", "PositionManager")
