"""Canonical trading vocabulary shared by every broker adapter.

Each broker names things differently (e.g. Zerodha's `NRML` vs. Angel
One's `CARRYFORWARD` both mean "margin/carry-forward product"). Adapters
translate between these canonical enums and each broker's wire format;
the rest of the application only ever sees these enums, never a broker's
raw strings.
"""

from enum import StrEnum


class BrokerName(StrEnum):
    """Identifies which adapter implementation to use.

    `PAPER` is `app.paper.broker.PaperBroker` — not a real brokerage,
    but a full `BrokerInterface` implementation in its own right (see
    that module's docstring), so it's named alongside the three real
    brokers rather than modeled as a separate concept.
    """

    ANGEL_ONE = "angel_one"
    UPSTOX = "upstox"
    ZERODHA = "zerodha"
    PAPER = "paper"


class Exchange(StrEnum):
    """Trading exchange / segment."""

    NSE = "NSE"
    BSE = "BSE"
    NFO = "NFO"  # NSE futures & options
    BFO = "BFO"  # BSE futures & options
    MCX = "MCX"
    CDS = "CDS"  # currency derivatives


class OrderSide(StrEnum):
    """Direction of a trade."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    """Pricing behavior of an order."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LOSS = "STOP_LOSS"
    STOP_LOSS_MARKET = "STOP_LOSS_MARKET"


class ProductType(StrEnum):
    """Margin/settlement treatment of a position."""

    INTRADAY = "INTRADAY"
    DELIVERY = "DELIVERY"
    MARGIN = "MARGIN"
    COVER_ORDER = "COVER_ORDER"
    BRACKET_ORDER = "BRACKET_ORDER"


class OrderVariety(StrEnum):
    """Order placement mode."""

    REGULAR = "REGULAR"
    AFTER_MARKET = "AFTER_MARKET"
    STOP_LOSS = "STOP_LOSS"
    ICEBERG = "ICEBERG"


class OrderValidity(StrEnum):
    """Order time-in-force."""

    DAY = "DAY"
    IMMEDIATE_OR_CANCEL = "IOC"
    GOOD_TILL_CANCELLED = "GTC"


class OrderStatus(StrEnum):
    """Canonical lifecycle state of an order."""

    PENDING = "PENDING"
    OPEN = "OPEN"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    TRIGGER_PENDING = "TRIGGER_PENDING"
    UNKNOWN = "UNKNOWN"


class HistoricalInterval(StrEnum):
    """Candle interval for historical OHLCV data."""

    ONE_MINUTE = "1minute"
    THREE_MINUTE = "3minute"
    FIVE_MINUTE = "5minute"
    TEN_MINUTE = "10minute"
    FIFTEEN_MINUTE = "15minute"
    THIRTY_MINUTE = "30minute"
    ONE_HOUR = "60minute"
    ONE_DAY = "day"
