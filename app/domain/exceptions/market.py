"""Market Data Ingestion exceptions.

Raised by `app.market.ingestion` when a broker's historical-data response
can't be turned into a usable candle series — either because it's empty
(no data for the requested symbol/range) or because a bar it returned
fails `app.market.dto.MarketCandle`'s own OHLCV validation.

Keeping these distinct from
`app.domain.exceptions.indicators.InvalidOHLCVDataError` matters:
that exception means "the Indicator Engine was handed a malformed
DataFrame it shouldn't have been asked to compute against"; these mean
"the broker itself didn't have anything (or anything valid) to give
us" — a different failure, at a different layer, that callers need to
be able to tell apart (e.g. to distinguish "wrong symbol" from "our own
computation is broken").
"""

from app.domain.enums.trading import BrokerName, Exchange, HistoricalInterval


class MarketDataError(Exception):
    """Base class for every market-data-ingestion failure."""


class NoHistoricalDataError(MarketDataError):
    """Raised when a broker's `historical_data` call succeeds but
    returns zero bars for the requested symbol/interval/date range —
    e.g. a market holiday, an incorrect instrument identifier, or a
    range with no trading activity.
    """

    def __init__(
        self,
        *,
        broker: BrokerName,
        exchange: Exchange,
        tradingsymbol: str,
        interval: HistoricalInterval,
    ) -> None:
        self.broker = broker
        self.exchange = exchange
        self.tradingsymbol = tradingsymbol
        self.interval = interval
        super().__init__(
            f"{broker.value}: no historical data returned for "
            f"{exchange.value}:{tradingsymbol} at {interval.value}"
        )


class InvalidHistoricalDataError(MarketDataError):
    """Raised when a broker returns a bar that fails
    `app.market.dto.MarketCandle`'s own OHLCV validation (e.g.
    non-positive prices, or `high`/`low` inconsistent with
    `open`/`close`) — a broker data-quality issue, not a bug in this
    codebase's own computation.
    """

    def __init__(
        self, *, broker: BrokerName, exchange: Exchange, tradingsymbol: str, reason: str
    ) -> None:
        self.broker = broker
        self.exchange = exchange
        self.tradingsymbol = tradingsymbol
        self.reason = reason
        super().__init__(
            f"{broker.value}: invalid historical bar for "
            f"{exchange.value}:{tradingsymbol}: {reason}"
        )
