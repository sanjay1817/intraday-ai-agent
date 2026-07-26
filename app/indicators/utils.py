"""Conversion helpers shared by concrete indicator implementations.

Kept separate from `app.indicators.base` so indicator modules import only
what they need; centralized here (rather than repeated per indicator) so
"how do we turn a pandas-ta NaN into a Pydantic-safe value" has exactly
one answer.
"""

import math
from typing import TypeVar

import pandas as pd

from app.domain.exceptions.indicators import InsufficientDataError
from app.indicators.base import IndicatorParams
from app.indicators.schemas import SingleValuePoint, SingleValueResult

_T = TypeVar("_T")


def clean_float(value: object) -> float | None:
    """Convert a pandas/numpy scalar to a plain float, mapping `NaN` to
    `None` — `NaN` has no valid JSON representation, so every indicator
    represents "not yet available" (warm-up periods, undefined ratios)
    as `None` instead.
    """

    if value is None:
        return None
    as_float = float(value)  # type: ignore[arg-type]
    return None if math.isnan(as_float) else as_float


def require_computed(value: _T | None, indicator_name: str) -> _T:
    """Raise `InsufficientDataError` if a pandas-ta function returned
    `None` — its documented signal for "not enough input rows to compute
    this indicator with these parameters" — instead of failing later
    with an opaque `AttributeError` on `None`.
    """

    if value is None:
        raise InsufficientDataError(indicator_name)
    return value


def series_to_single_value_result(
    name: str, params: IndicatorParams, series: pd.Series
) -> SingleValueResult:
    """Build a `SingleValueResult` from one numeric pandas Series.

    Shared by every indicator whose output is one value per bar (SMA,
    EMA, RSI, ATR, VWAP, Volume SMA).
    """

    values = [SingleValuePoint(timestamp=ts, value=clean_float(v)) for ts, v in series.items()]
    return SingleValueResult(name=name, params=params.model_dump(), values=values)
