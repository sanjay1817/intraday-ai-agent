"""Technical Indicator Engine exceptions.

Raised by `app.indicators.engine.IndicatorEngine` and by individual
indicators (`app.indicators.*`) so callers can distinguish "you asked for
something that doesn't exist" from "your data doesn't support this
calculation" from "there isn't enough data yet".
"""


class IndicatorError(Exception):
    """Base class for every technical-indicator-engine failure."""


class UnknownIndicatorError(IndicatorError):
    """Raised when `IndicatorRequest.name` has no registered indicator.

    Carries `available` (the currently registered indicator names) so
    callers/logs can suggest a correction without a second lookup.
    """

    def __init__(self, requested: str, available: list[str]) -> None:
        self.requested = requested
        self.available = available
        super().__init__(
            f"Unknown indicator {requested!r}. Registered indicators: {', '.join(available)}"
        )


class InvalidOHLCVDataError(IndicatorError):
    """Raised when the input DataFrame isn't valid OHLCV data: missing
    required columns, empty, not indexed by an ordered `DatetimeIndex`
    (required by session-anchored indicators like VWAP), or similar.
    """


class InsufficientDataError(IndicatorError):
    """Raised when the DataFrame has too few rows for the requested
    indicator/parameters to produce any value (pandas-ta signals this by
    returning `None` instead of a Series/DataFrame).
    """

    def __init__(self, indicator_name: str) -> None:
        self.indicator_name = indicator_name
        super().__init__(
            f"Not enough OHLCV rows to compute {indicator_name!r} with the requested parameters"
        )
