"""Historical Backtesting exceptions.

Distinct from `app.domain.exceptions.market.MarketDataError` (a broker
data-quality/availability failure): these mean the *request itself* is
invalid for a backtest — a future date, an inverted time range — caught
before any broker call is made.
"""


class BacktestError(Exception):
    """Base class for every historical-backtest failure."""


class FutureDateError(BacktestError):
    """Raised when a backtest is requested for a date that hasn't fully
    closed yet — a backtest can only replay a session that already
    happened.
    """


class OptionHistoricalDataUnavailableError(BacktestError):
    """Raised when the resolved option contract has no historical data
    available from the broker — reported to the caller as a known
    limitation, never silently substituted with the underlying's price.
    """
