"""Paper Trading Engine state models: orders, fills, positions, closed
trades, and the portfolio snapshot.

Every model here is a frozen, point-in-time value object — matching this
codebase's established pattern (`app.strategy.models`, `app.paper.dto`):
the stateful engine (`app.paper.order_manager`/`position_manager`/
`portfolio`) owns mutable dicts internally and produces a *new* instance
via `model_copy(update=...)` on every state change, rather than mutating
one of these in place. That keeps every snapshot returned to a caller
(or asserted against in a test) immutable and safe to hold onto.

Reuses `Exchange`, `OrderSide`, `OrderStatus` from
`app.domain.enums.trading` and `PaperOrderType` from `app.paper.dto` —
the same universal vocabulary the broker layer and this package's own
request DTOs already use — rather than declaring parallel enums.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums.trading import Exchange, OrderSide, OrderStatus, OrderValidity
from app.paper.dto import PaperOrderType


class TradeMetadata(BaseModel):
    """The strategy provenance behind a trade, when it has any.

    Populated only when a caller supplies it at order-placement time
    (e.g. a future orchestrator acting on an `InstructorRecommendation`)
    — a manually/API-placed order has none of this, and every field is
    therefore optional rather than required. Carried on `PaperOrder` and
    forwarded onto `ClosedTrade` when a position closes, so a completed
    trade's full "why" survives past the order that opened it.
    """

    model_config = ConfigDict(frozen=True)

    confidence: float | None = Field(default=None, ge=0, le=100)
    agreeing_strategies: list[str] = Field(default_factory=list)
    conflicting_strategies: list[str] = Field(default_factory=list)
    indicators_used: list[str] = Field(default_factory=list)
    reasoning: str | None = None


class PaperOrder(BaseModel):
    """The full lifecycle record of one simulated order.

    `parent_order_id`/`oco_group_id` support the bracket-order design
    `app.paper.dto.PaperOrderRequest` already documents: a stop-loss/
    target pair spawned once an entry fills shares one `oco_group_id`
    (filling one cancels the other) and both point back to the entry via
    `parent_order_id`. Plain, non-bracket orders leave both `None`.
    """

    model_config = ConfigDict(frozen=True)

    order_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    exchange: Exchange
    side: OrderSide
    order_type: PaperOrderType
    quantity: int = Field(gt=0)
    filled_quantity: int = Field(default=0, ge=0)
    price: float | None = Field(default=None, gt=0)
    trigger_price: float | None = Field(default=None, gt=0)
    trailing_amount: float | None = Field(default=None, gt=0)
    trailing_percent: float | None = Field(default=None, gt=0, le=100)
    average_fill_price: float | None = Field(default=None, gt=0)
    status: OrderStatus
    validity: OrderValidity = OrderValidity.DAY
    expires_at: datetime | None = None
    stop_loss_price: float | None = Field(default=None, gt=0)
    target_price: float | None = Field(default=None, gt=0)
    parent_order_id: str | None = None
    oco_group_id: str | None = None
    tag: str | None = None
    metadata: TradeMetadata | None = None
    status_message: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _check_fill_state_is_consistent(self) -> "PaperOrder":
        """`filled_quantity` can never exceed `quantity`, and a
        `COMPLETE` order must actually be fully filled with a known
        average price — two independent fields that must never drift
        apart in this engine's own bookkeeping.
        """

        if self.filled_quantity > self.quantity:
            raise ValueError(
                f"filled_quantity ({self.filled_quantity}) cannot exceed quantity ({self.quantity})"
            )

        if self.status is OrderStatus.COMPLETE:
            if self.filled_quantity != self.quantity:
                raise ValueError("COMPLETE order must have filled_quantity == quantity")
            if self.average_fill_price is None:
                raise ValueError("COMPLETE order must have an average_fill_price")

        return self


class Fill(BaseModel):
    """One execution against an order.

    An internal engine/`order_manager` record, not itself an API
    response shape — `ClosedTrade` is the user-facing "trade history"
    entry (see its own docstring). Kept as its own model rather than
    inlined so a future partial-fill order (several fills summing to one
    order's `filled_quantity`) has somewhere to record each execution.
    """

    model_config = ConfigDict(frozen=True)

    fill_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    exchange: Exchange
    side: OrderSide
    quantity: int = Field(gt=0)
    price: float = Field(gt=0)
    timestamp: datetime


class PaperPosition(BaseModel):
    """One symbol's current open position.

    `quantity` is signed: positive is long, negative is short — there is
    no separate "side" field, since the sign already says which. A flat
    position (`quantity == 0`) is not expected to be kept around by
    `app.paper.position_manager` (it removes the entry entirely instead),
    but is not itself invalid here.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    exchange: Exchange
    quantity: int
    average_price: float = Field(ge=0)
    last_price: float = Field(gt=0)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    opened_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _check_average_price_is_set_when_open(self) -> "PaperPosition":
        """A non-flat position must have a real (positive) cost basis —
        `average_price == 0` is only meaningful for a flat position.
        """

        if self.quantity != 0 and self.average_price <= 0:
            raise ValueError("a non-flat position must have average_price > 0")
        return self

    @model_validator(mode="after")
    def _check_unrealized_pnl_matches_prices(self) -> "PaperPosition":
        """`unrealized_pnl` must equal mark-to-market against
        `last_price` for the current `quantity`/`average_price` — this
        engine never lets the two drift apart, so a mismatch here is a
        bug in whichever caller constructed this, not a legitimate state.
        """

        expected = (self.last_price - self.average_price) * self.quantity
        if abs(self.unrealized_pnl - expected) > 1e-6:
            raise ValueError(
                f"unrealized_pnl ({self.unrealized_pnl}) does not match "
                f"(last_price - average_price) * quantity ({expected})"
            )
        return self


class ClosedTrade(BaseModel):
    """One completed round-trip (a position fully or partially closed) —
    this engine's "trade history" entry, and what `GET /api/v1/paper/trades`
    returns. Carries the full metadata this milestone requires: symbol,
    side, entry/exit price and timestamp, quantity, stop-loss/target,
    realized P&L, and (when available) the strategy provenance behind it.
    """

    model_config = ConfigDict(frozen=True)

    trade_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    exchange: Exchange
    side: OrderSide
    quantity: int = Field(gt=0)
    entry_price: float = Field(gt=0)
    exit_price: float = Field(gt=0)
    stop_loss_price: float | None = Field(default=None, gt=0)
    target_price: float | None = Field(default=None, gt=0)
    pnl: float
    entry_order_id: str = Field(min_length=1)
    exit_order_id: str = Field(min_length=1)
    entry_timestamp: datetime
    exit_timestamp: datetime
    metadata: TradeMetadata | None = None

    @model_validator(mode="after")
    def _check_exit_is_not_before_entry(self) -> "ClosedTrade":
        if self.exit_timestamp < self.entry_timestamp:
            raise ValueError("exit_timestamp cannot be before entry_timestamp")
        return self

    @model_validator(mode="after")
    def _check_pnl_matches_side_and_prices(self) -> "ClosedTrade":
        """`side` is the entry side: `BUY` means this was a long trade
        (profits when price rises), `SELL` means a short trade (profits
        when price falls) — the same two formulas
        `app.paper.position_manager` must use to realize P&L on a close,
        checked again here so the two can never silently diverge.
        """

        if self.side is OrderSide.BUY:
            expected = (self.exit_price - self.entry_price) * self.quantity
        else:  # OrderSide.SELL
            expected = (self.entry_price - self.exit_price) * self.quantity

        if abs(self.pnl - expected) > 1e-6:
            raise ValueError(f"pnl ({self.pnl}) does not match the expected value ({expected})")
        return self


class Portfolio(BaseModel):
    """The account-level snapshot `GET /api/v1/paper/portfolio` returns —
    the single source of truth for cash, equity, and P&L that
    `app.paper.portfolio.PortfolioManager` produces.

    `cash` already reflects every fill's cost/proceeds (buying debits
    it, selling credits it) — it is not "cash plus cost basis", it is
    the actual spendable cash left. `available_cash` is `cash` minus
    whatever `PortfolioManager` currently has reserved against resting
    orders that haven't filled yet (not itself a field here — a resting
    order is this package's `order_manager`/`engine` concern, not
    something this snapshot's data alone can reconstruct); it is
    therefore never more than `cash`, but neither field is derivable
    from the other without knowing that reserved amount. `used_capital`
    is the cost basis currently deployed across every open position,
    long or short alike (`abs(quantity) * average_price` per position)
    — capital genuinely at risk, not spendable. `equity` is `cash` plus
    the *current market value* of every open position (not just their
    unrealized P&L), and `total_pnl` is simply `realized_pnl +
    unrealized_pnl` restated as its own field since a caller displaying
    "how am I doing overall" shouldn't have to add two numbers itself.
    """

    model_config = ConfigDict(frozen=True)

    cash: float
    available_cash: float
    used_capital: float
    initial_capital: float = Field(gt=0)
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    equity: float
    positions: list[PaperPosition] = Field(default_factory=list)
    updated_at: datetime

    @model_validator(mode="after")
    def _check_available_cash_is_within_cash(self) -> "Portfolio":
        if not (0 <= self.available_cash <= self.cash + 1e-6):
            raise ValueError(
                f"available_cash ({self.available_cash}) must be between 0 and cash ({self.cash})"
            )
        return self

    @model_validator(mode="after")
    def _check_used_capital_matches_positions(self) -> "Portfolio":
        expected = sum(
            abs(position.quantity) * position.average_price for position in self.positions
        )
        if abs(self.used_capital - expected) > 1e-6:
            raise ValueError(
                f"used_capital ({self.used_capital}) does not match the sum of "
                f"position cost bases ({expected})"
            )
        return self

    @model_validator(mode="after")
    def _check_unrealized_pnl_matches_positions(self) -> "Portfolio":
        expected = sum(position.unrealized_pnl for position in self.positions)
        if abs(self.unrealized_pnl - expected) > 1e-6:
            raise ValueError(
                f"unrealized_pnl ({self.unrealized_pnl}) does not match "
                f"the sum of position unrealized_pnl ({expected})"
            )
        return self

    @model_validator(mode="after")
    def _check_total_pnl_matches_realized_plus_unrealized(self) -> "Portfolio":
        expected = self.realized_pnl + self.unrealized_pnl
        if abs(self.total_pnl - expected) > 1e-6:
            raise ValueError(
                f"total_pnl ({self.total_pnl}) does not match "
                f"realized_pnl + unrealized_pnl ({expected})"
            )
        return self

    @model_validator(mode="after")
    def _check_equity_matches_cash_plus_positions_market_value(self) -> "Portfolio":
        market_value = sum(position.quantity * position.last_price for position in self.positions)
        expected = self.cash + market_value
        if abs(self.equity - expected) > 1e-6:
            raise ValueError(
                f"equity ({self.equity}) does not match cash + positions' "
                f"market value ({expected})"
            )
        return self
