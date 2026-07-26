"""Market Data Engine DTOs.

Every other module in `app.market` passes these — never a raw broker
payload, never an ORM row — between providers, the tick processor, the
candle builder, the cache, and callers outside this package.

Reuses `Exchange`/`HistoricalInterval` from `app.domain.enums.trading`
(introduced for the broker layer) rather than inventing parallel enums:
a 5-minute candle here and a 5-minute `historical_data()` request to a
broker adapter are the same concept and should say so.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums.trading import Exchange, HistoricalInterval


class Tick(BaseModel):
    """One live market-data update for an instrument.

    `bid`/`ask` are optional because not every feed mode (e.g. an
    LTP-only subscription) includes them; `ltp` and `volume` are always
    required — a tick without a traded price or a total-traded-volume
    figure isn't a usable tick.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    exchange: Exchange
    ltp: float = Field(gt=0)
    bid: float | None = Field(default=None, gt=0)
    ask: float | None = Field(default=None, gt=0)
    volume: int = Field(ge=0)
    timestamp: datetime

    @model_validator(mode="after")
    def _check_bid_ask_spread(self) -> "Tick":
        """A crossed quote (bid above ask) signals a corrupted or
        stale-leg tick, not a real market state.
        """

        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError(f"bid ({self.bid}) cannot exceed ask ({self.ask})")
        return self


class MarketCandle(BaseModel):
    """One OHLCV bar built (or downloaded) for one symbol/interval.

    `timestamp` is the bar's *open* time (its bucket start), matching the
    convention `app.brokers.BrokerInterface.historical_data` already
    uses. `is_final` distinguishes a still-forming bar (the current,
    incomplete bucket `app.market.candle_builder` is accumulating ticks
    into) from a closed one; `tick_count` is a diagnostic for "did this
    candle actually see any ticks, or is it a synthetic gap-fill".
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    exchange: Exchange
    interval: HistoricalInterval
    timestamp: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int = Field(ge=0)
    tick_count: int = Field(default=0, ge=0)
    is_final: bool = True

    @model_validator(mode="after")
    def _check_ohlc_consistency(self) -> "MarketCandle":
        """`low`/`high` must actually bound `open`/`close` — a violation
        here means upstream tick data or candle aggregation is broken,
        not a legitimate (if unusual) market move.
        """

        if self.low > self.high:
            raise ValueError(f"low ({self.low}) cannot exceed high ({self.high})")
        if not self.low <= self.open <= self.high:
            raise ValueError(f"open ({self.open}) is outside [low, high] ({self.low}, {self.high})")
        if not self.low <= self.close <= self.high:
            raise ValueError(
                f"close ({self.close}) is outside [low, high] ({self.low}, {self.high})"
            )
        return self


class MarketSessionState(StrEnum):
    """A trading day's lifecycle, plus the two states outside it.

    `HOLIDAY` is a specifically-known non-trading *day* (from the
    exchange's holiday calendar); `CLOSED` is the catch-all for "no
    session is active right now" that isn't a holiday — nights and
    weekends on an otherwise normal trading day. The spec this engine
    implements doesn't name that second case, but `market_session.py`'s
    automatic detection has to resolve to *something* outside
    PRE_OPEN/OPEN/LUNCH/CLOSING/AFTER_MARKET/HOLIDAY, so it's added here
    rather than silently misreporting one of those six.
    """

    PRE_OPEN = "PRE_OPEN"
    OPEN = "OPEN"
    LUNCH = "LUNCH"
    CLOSING = "CLOSING"
    AFTER_MARKET = "AFTER_MARKET"
    HOLIDAY = "HOLIDAY"
    CLOSED = "CLOSED"


class SubscriptionState(StrEnum):
    """Whether `app.market.symbol_manager` should currently be streaming
    a symbol's ticks.
    """

    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"


class SymbolMetadata(BaseModel):
    """Static reference data for one tradable instrument.

    `instrument_token` mirrors the same documented gap as the broker
    layer: several brokers require a numeric/composite instrument
    identifier (not the plain trading symbol) for market-data and order
    calls, resolved by an instrument-master lookup this engine doesn't
    implement — `None` until that lookup exists upstream.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    exchange: Exchange
    tradingsymbol: str = Field(min_length=1)
    instrument_token: str | None = None
    lot_size: int = Field(default=1, gt=0)
    tick_size: float = Field(default=0.05, gt=0)


class WatchlistEntry(BaseModel):
    """One symbol's subscription state within `app.market.symbol_manager`."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    exchange: Exchange
    state: SubscriptionState


class HistoricalDownloadRequest(BaseModel):
    """One request to `app.market.historical_service` for a symbol's
    historical candles over a date range, at one interval.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    exchange: Exchange
    interval: HistoricalInterval
    from_date: datetime
    to_date: datetime

    @model_validator(mode="after")
    def _check_date_range(self) -> "HistoricalDownloadRequest":
        if self.from_date >= self.to_date:
            raise ValueError("from_date must be before to_date")
        return self


class ProviderMetrics(BaseModel):
    """A point-in-time health/performance snapshot for one market-data
    provider connection — what `app.market.manager` exposes for the
    monitoring responsibilities (latency, dropped ticks, reconnects,
    uptime, queue size, processing time) this engine is required to track.
    """

    model_config = ConfigDict(frozen=True)

    provider: str = Field(min_length=1)
    connected: bool
    uptime_seconds: float = Field(ge=0)
    reconnect_count: int = Field(default=0, ge=0)
    dropped_tick_count: int = Field(default=0, ge=0)
    queue_size: int = Field(default=0, ge=0)
    last_tick_latency_ms: float | None = Field(default=None, ge=0)
    average_processing_time_ms: float | None = Field(default=None, ge=0)
    last_heartbeat_at: datetime | None = None
