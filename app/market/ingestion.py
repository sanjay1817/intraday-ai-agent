"""Market Data Ingestion: fetches recent OHLCV candles for one
instrument from a broker and converts them into this codebase's
canonical `MarketCandle` shape, plus a lightweight NSE/BSE session-state
check.

Deliberately narrow: this is a single, best-effort read of "what have
the candles looked like recently", not `app.market`'s originally-planned
`candle_builder.py`/`tick_processor.py`/`websocket_manager.py` (a live
tick-to-candle streaming pipeline) — the Intraday AI Instructor works
on-demand (fetch, analyze, recommend), not as a continuously-running
background service, so none of that infrastructure is needed here.

Session-state detection is a plain weekday/time-window check against
NSE/BSE's regular 09:15-15:30 IST equity session — not the full
`market_session.py` this project's foundation once planned, which would
also need an exchange holiday calendar this codebase has no data source
for. `HOLIDAY` is therefore never returned; a holiday reads as `CLOSED`
(the same as any other non-trading day), which is honest given what
this function can actually know.

This module deliberately depends only on `BrokerInterface`, never on
`BaseBrokerAdapter` or a concrete adapter — `broker_name` is accepted as
its own parameter rather than read off the broker instance, since
`BrokerInterface` itself declares no such attribute (only the shared
adapter base class does), and reaching past the interface for it would
break the exact abstraction boundary `app.brokers.factory`'s own
docstring establishes.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.brokers.base import BrokerInterface
from app.domain.enums.trading import BrokerName, Exchange, HistoricalInterval
from app.domain.exceptions.market import InvalidHistoricalDataError, NoHistoricalDataError
from app.market.dto import MarketCandle, MarketSessionState

_INDIA_TZ = ZoneInfo("Asia/Kolkata")
_SESSION_OPEN = time(9, 15)
_SESSION_CLOSE = time(15, 30)

#: Lookback window sized generously so indicator warm-up periods (e.g.
#: Ichimoku's ~52-bar requirement) are satisfied regardless of
#: weekends/holidays inside the window — not an attempt to model the
#: exchange calendar precisely.
_INTRADAY_LOOKBACK_DAYS = 10
_DAILY_LOOKBACK_DAYS = 365


def _default_lookback_days(interval: HistoricalInterval) -> int:
    """The lookback window `fetch_recent_candles` uses when the caller
    doesn't specify one.
    """

    if interval is HistoricalInterval.ONE_DAY:
        return _DAILY_LOOKBACK_DAYS
    return _INTRADAY_LOOKBACK_DAYS


def current_session_state(now: datetime | None = None) -> MarketSessionState:
    """A best-effort NSE/BSE equity session state for `now` (default:
    the current time).

    Only ever returns `OPEN`, `PRE_OPEN`, or `CLOSED` — this is a plain
    weekday/time-window check with no exchange holiday calendar behind
    it, so a holiday reads as `CLOSED` rather than `HOLIDAY`. `LUNCH`/
    `CLOSING`/`AFTER_MARKET` are never returned either: NSE/BSE's
    regular equity session has no lunch break, and this function isn't
    trying to model the closing-auction/after-market windows precisely.
    """

    moment = (now or datetime.now(UTC)).astimezone(_INDIA_TZ)

    if moment.weekday() >= 5:  # Saturday, Sunday
        return MarketSessionState.CLOSED

    current_time = moment.time()
    if current_time < _SESSION_OPEN:
        return MarketSessionState.PRE_OPEN
    if current_time < _SESSION_CLOSE:
        return MarketSessionState.OPEN
    return MarketSessionState.CLOSED


async def fetch_recent_candles(
    broker: BrokerInterface,
    broker_name: BrokerName,
    exchange: Exchange,
    tradingsymbol: str,
    interval: HistoricalInterval,
    *,
    lookback_days: int | None = None,
    now: datetime | None = None,
) -> list[MarketCandle]:
    """Fetch and convert the most recent OHLCV candles for one instrument.

    Args:
        broker: The adapter to fetch through — any `BrokerInterface`
            implementation; this function is broker-agnostic by design.
        broker_name: Identifies `broker` for error reporting; not read
            off `broker` itself (see this module's docstring).
        exchange: Which exchange/segment the instrument trades on.
        tradingsymbol: The identifier `broker`'s adapter expects for
            this instrument (a broker-specific instrument token for
            Angel One/Zerodha, an `"EXCHANGE|ISIN"`-shaped key for
            Upstox) — this function does no symbol resolution of its
            own; the caller supplies whatever form the configured
            broker expects.
        interval: The candle interval to fetch.
        lookback_days: How many calendar days of history to request;
            defaults to a generous, interval-appropriate window (see
            `_default_lookback_days`) sized for indicator warm-up, not
            for a specific analysis window.
        now: The current time, for computing the fetch window and for
            deterministic testing; defaults to the real current time.

    Returns:
        Candles in ascending timestamp order.

    Raises:
        NoHistoricalDataError: the broker returned zero bars.
        InvalidHistoricalDataError: a returned bar fails `MarketCandle`'s
            own OHLCV validation.
    """

    resolved_now = now or datetime.now(UTC)
    resolved_lookback_days = (
        lookback_days if lookback_days is not None else _default_lookback_days(interval)
    )
    from_date = resolved_now - timedelta(days=resolved_lookback_days)

    bars = await broker.historical_data(exchange, tradingsymbol, interval, from_date, resolved_now)

    if not bars:
        raise NoHistoricalDataError(
            broker=broker_name, exchange=exchange, tradingsymbol=tradingsymbol, interval=interval
        )

    candles: list[MarketCandle] = []
    for bar in bars:
        try:
            candles.append(
                MarketCandle(
                    symbol=tradingsymbol,
                    exchange=exchange,
                    interval=interval,
                    timestamp=bar.timestamp,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                )
            )
        except ValidationError as exc:
            raise InvalidHistoricalDataError(
                broker=broker_name,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                reason=str(exc),
            ) from exc

    candles.sort(key=lambda candle: candle.timestamp)
    return candles
