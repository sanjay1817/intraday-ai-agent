"""Paper Trading Engine input DTOs: order placement and modification
requests.

The Paper Trading Engine simulates a real exchange, so its order model
is intentionally richer than any single broker adapter's. Bracket and
OCO orders are exchange/OMS-level constructs — most Indian broker APIs
don't natively expose them as one wire-level order type; they're built
client-side over plain market/limit/stop orders (which is exactly what
`app.brokers.BrokerInterface.place_order` sends). That richer construct
therefore belongs to this engine, not to `app.domain.entities.broker`.

Reuses `Exchange`, `OrderSide`, `OrderValidity` from
`app.domain.enums.trading` — universal concepts already modeled for the
broker layer — rather than re-declaring them.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums.trading import Exchange, OrderSide, OrderValidity


class PaperOrderType(StrEnum):
    """Pricing/trigger behavior of a simulated order.

    Deliberately distinct from `app.domain.enums.trading.OrderType`:
    that enum models what a real broker's REST API accepts on the wire.
    `TRAILING_STOP` here is a client-side-simulated construct no broker
    adapter sends as a single order type, so it belongs to this engine's
    own vocabulary rather than being retrofitted onto the broker layer's.
    """

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    TRAILING_STOP = "TRAILING_STOP"


class PaperOrderRequest(BaseModel):
    """A request to place one simulated order.

    Bracket orders aren't modeled as a separate order type: setting
    `stop_loss_price`/`target_price` on any entry order tells
    `app.paper.order_manager` to spawn the two exit legs — linked to
    each other as an OCO pair (filling one cancels the other) and to
    this order as their parent — once this entry fills. This mirrors how
    real bracket orders are actually built on top of plain orders, not a
    distinct wire-level order type, and lets any entry (`MARKET`,
    `LIMIT`, `STOP`, ...) become a bracket order the same way.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    exchange: Exchange
    side: OrderSide
    order_type: PaperOrderType
    quantity: int = Field(gt=0)
    price: float | None = Field(default=None, gt=0)
    trigger_price: float | None = Field(default=None, gt=0)
    trailing_amount: float | None = Field(default=None, gt=0)
    trailing_percent: float | None = Field(default=None, gt=0, le=100)
    validity: OrderValidity = OrderValidity.DAY
    expires_at: datetime | None = None
    stop_loss_price: float | None = Field(default=None, gt=0)
    target_price: float | None = Field(default=None, gt=0)
    tag: str | None = None

    @model_validator(mode="after")
    def _check_price_fields_match_order_type(self) -> "PaperOrderRequest":
        """Each order type requires exactly the price fields that give
        it meaning — a LIMIT with no limit price, or a TRAILING_STOP
        with neither a trailing amount nor percent, isn't a placeable
        order.
        """

        if (
            self.order_type in (PaperOrderType.LIMIT, PaperOrderType.STOP_LIMIT)
            and self.price is None
        ):
            raise ValueError(f"{self.order_type} requires `price`")

        if (
            self.order_type in (PaperOrderType.STOP, PaperOrderType.STOP_LIMIT)
            and self.trigger_price is None
        ):
            raise ValueError(f"{self.order_type} requires `trigger_price`")

        if self.order_type is PaperOrderType.TRAILING_STOP:
            has_amount = self.trailing_amount is not None
            has_percent = self.trailing_percent is not None
            if has_amount == has_percent:  # neither set, or both set
                raise ValueError(
                    "TRAILING_STOP requires exactly one of trailing_amount/trailing_percent"
                )

        return self

    @model_validator(mode="after")
    def _check_bracket_legs_are_directionally_consistent(self) -> "PaperOrderRequest":
        """A bracket's stop-loss/target only make sense on the correct
        side of the entry price — this can only be checked when `price`
        is known upfront (LIMIT/STOP_LIMIT); MARKET/STOP entries fill at
        a price only discovered at match time, so `app.paper.order_manager`
        validates those brackets once the entry actually fills.
        """

        if self.price is None or (self.stop_loss_price is None and self.target_price is None):
            return self

        if self.side is OrderSide.BUY:
            if self.stop_loss_price is not None and not self.stop_loss_price < self.price:
                raise ValueError("BUY stop_loss_price must be below price")
            if self.target_price is not None and not self.target_price > self.price:
                raise ValueError("BUY target_price must be above price")
        else:  # OrderSide.SELL
            if self.stop_loss_price is not None and not self.stop_loss_price > self.price:
                raise ValueError("SELL stop_loss_price must be above price")
            if self.target_price is not None and not self.target_price < self.price:
                raise ValueError("SELL target_price must be below price")

        return self


class OrderModificationRequest(BaseModel):
    """A request to modify an existing, still-open simulated order.

    PATCH-style: only the fields a caller wants to change are set,
    `None` means "leave as-is" — never a full replacement of the
    original `PaperOrderRequest`.
    """

    model_config = ConfigDict(frozen=True)

    order_id: str = Field(min_length=1)
    quantity: int | None = Field(default=None, gt=0)
    price: float | None = Field(default=None, gt=0)
    trigger_price: float | None = Field(default=None, gt=0)
    validity: OrderValidity | None = None
    expires_at: datetime | None = None
