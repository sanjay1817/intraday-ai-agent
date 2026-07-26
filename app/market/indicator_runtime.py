"""Indicator Runtime Integration: adapts `app.market`'s `MarketCandle`
DTOs into the pandas `DataFrame` shape `app.indicators.IndicatorEngine`
requires, and runs it.

This is the one seam between the Market Data and Indicator Engine
bounded contexts. It carries no knowledge of *which* indicators any
particular strategy needs — that's each `BaseStrategy`'s own
responsibility (see `app.strategy.base_strategy`) — so this stays a
thin, generic, reusable adapter rather than hard-coding a strategy-
specific indicator set here.
"""

from collections.abc import Sequence
from typing import Any

import pandas as pd

from app.indicators.engine import IndicatorEngine, IndicatorRequest
from app.indicators.schemas import IndicatorResult
from app.market.dto import MarketCandle


def compute_indicators(
    engine: IndicatorEngine,
    candles: Sequence[MarketCandle],
    requests: Sequence[IndicatorRequest],
) -> dict[str, IndicatorResult[Any]]:
    """Compute every indicator in `requests` against `candles`.

    Args:
        engine: The `IndicatorEngine` instance to compute through — the
            caller controls its lifetime (a fresh instance per pipeline
            run is the safe default; see the architecture audit's note
            on `IndicatorEngine`'s cache not being thread-safe).
        candles: OHLCV history for one symbol/timeframe, in ascending
            timestamp order (as `app.market.ingestion.fetch_recent_candles`
            already returns them).
        requests: Which indicators to compute — only these, never a
            fixed, hard-coded set.

    Returns:
        A dict keyed by each request's `result_key`, exactly as
        `IndicatorEngine.calculate` returns it.

    Raises:
        InvalidOHLCVDataError: `candles` is empty (propagates from
            `IndicatorEngine.calculate`'s own validation).
        UnknownIndicatorError: a request names an unregistered indicator.
    """

    df = _candles_to_dataframe(candles)
    return engine.calculate(df, requests)


def _candles_to_dataframe(candles: Sequence[MarketCandle]) -> pd.DataFrame:
    """Build the OHLCV `DataFrame` `IndicatorEngine.calculate` requires:
    `open`/`high`/`low`/`close`/`volume` columns indexed by an ascending
    `DatetimeIndex`.
    """

    index = pd.DatetimeIndex([candle.timestamp for candle in candles])
    return pd.DataFrame(
        {
            "open": [candle.open for candle in candles],
            "high": [candle.high for candle in candles],
            "low": [candle.low for candle in candles],
            "close": [candle.close for candle in candles],
            "volume": [candle.volume for candle in candles],
        },
        index=index,
    )
