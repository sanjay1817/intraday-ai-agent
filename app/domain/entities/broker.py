"""Broker domain entities.

Plain, immutable dataclasses that every `BrokerInterface` method returns
or accepts. Keeping these framework-agnostic (no Pydantic/ORM) means the
broker abstraction has zero dependency on the web or persistence layers —
only on the domain enums in `app.domain.enums.trading`.
"""

from dataclasses import dataclass, field
from datetime import datetime

from app.domain.enums.trading import (
    BrokerName,
    Exchange,
    OrderSide,
    OrderStatus,
    OrderType,
    OrderValidity,
    OrderVariety,
    ProductType,
)


@dataclass(frozen=True, slots=True)
class TokenBundle:
    """Authentication tokens returned by a broker's login/refresh flow.

    `feed_token` is only populated by brokers (e.g. Angel One) that issue
    a separate token for market-data websockets; it is `None` otherwise.
    """

    access_token: str
    refresh_token: str | None = None
    feed_token: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BrokerProfile:
    """The logged-in trading account's identity and entitlements."""

    broker: BrokerName
    client_id: str
    display_name: str
    email: str | None = None
    exchanges_enabled: tuple[Exchange, ...] = ()
    products_enabled: tuple[ProductType, ...] = ()


@dataclass(frozen=True, slots=True)
class BrokerFunds:
    """Available margin/cash for the trading account.

    `raw` retains the broker's unmapped response so callers needing a
    broker-specific figure (e.g. "span margin") aren't blocked on the
    canonical fields below.
    """

    broker: BrokerName
    available_cash: float
    used_margin: float
    total_balance: float
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Position:
    """An open intraday or carry-forward position."""

    tradingsymbol: str
    exchange: Exchange
    product: ProductType
    quantity: int
    average_price: float
    last_price: float
    pnl: float


@dataclass(frozen=True, slots=True)
class Holding:
    """A settled, delivery (T+1) holding in the demat account."""

    tradingsymbol: str
    exchange: Exchange
    isin: str
    quantity: int
    average_price: float
    last_price: float
    pnl: float


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """Parameters to place or modify an order.

    Fields that don't apply to every order type (`price`, `trigger_price`)
    default to `None`; adapters omit them from the wire payload when unset
    rather than sending broker-specific sentinel values.
    """

    tradingsymbol: str
    exchange: Exchange
    transaction_type: OrderSide
    quantity: int
    order_type: OrderType
    product: ProductType
    variety: OrderVariety = OrderVariety.REGULAR
    validity: OrderValidity = OrderValidity.DAY
    price: float | None = None
    trigger_price: float | None = None
    tag: str | None = None


@dataclass(frozen=True, slots=True)
class OrderResponse:
    """Acknowledgement returned immediately after placing/modifying/cancelling."""

    order_id: str
    broker: BrokerName
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OrderDetail:
    """A single row from the order book (`get_orders`)."""

    order_id: str
    tradingsymbol: str
    exchange: Exchange
    status: OrderStatus
    transaction_type: OrderSide
    order_type: OrderType
    product: ProductType
    quantity: int
    filled_quantity: int
    pending_quantity: int
    price: float
    average_price: float
    order_timestamp: datetime | None = None
    status_message: str | None = None


@dataclass(frozen=True, slots=True)
class Quote:
    """Last-traded-price snapshot for one instrument."""

    tradingsymbol: str
    exchange: Exchange
    last_price: float
    timestamp: datetime | None = None


@dataclass(frozen=True, slots=True)
class HistoricalBar:
    """A single OHLCV candle."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
