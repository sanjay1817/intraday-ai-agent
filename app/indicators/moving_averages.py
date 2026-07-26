"""Moving-average family: SMA, EMA, and Volume SMA.

All three share `LengthParams` (a single `length` field) and pandas-ta's
plain `sma`/`ema` functions — Volume SMA is simply `sma` run against the
`volume` column instead of `close`, so it needs no separate pandas-ta
call, just a different input Series.
"""

import pandas as pd
import pandas_ta as ta

from app.indicators.base import Indicator, LengthParams, register_indicator
from app.indicators.schemas import SingleValuePoint, SingleValueResult
from app.indicators.utils import require_computed, series_to_single_value_result


@register_indicator
class SMAIndicator(Indicator[LengthParams, SingleValuePoint]):
    """Simple Moving Average of `close`."""

    name = "SMA"
    params_model = LengthParams

    def compute(self, df: pd.DataFrame, params: LengthParams) -> pd.DataFrame:
        series = ta.sma(df["close"], length=params.length, talib=False)
        series = require_computed(series, self.name)
        return series.to_frame(name="value")

    def to_result(self, raw: pd.DataFrame, params: LengthParams) -> SingleValueResult:
        return series_to_single_value_result(self.name, params, raw["value"])


@register_indicator
class EMAIndicator(Indicator[LengthParams, SingleValuePoint]):
    """Exponential Moving Average of `close`."""

    name = "EMA"
    params_model = LengthParams

    def compute(self, df: pd.DataFrame, params: LengthParams) -> pd.DataFrame:
        series = ta.ema(df["close"], length=params.length, talib=False)
        series = require_computed(series, self.name)
        return series.to_frame(name="value")

    def to_result(self, raw: pd.DataFrame, params: LengthParams) -> SingleValueResult:
        return series_to_single_value_result(self.name, params, raw["value"])


@register_indicator
class VolumeSMAIndicator(Indicator[LengthParams, SingleValuePoint]):
    """Simple Moving Average of `volume` (not `close`) — smooths volume
    to reveal above/below-average participation.
    """

    name = "VOLUME_SMA"
    params_model = LengthParams

    def compute(self, df: pd.DataFrame, params: LengthParams) -> pd.DataFrame:
        series = ta.sma(df["volume"], length=params.length, talib=False)
        series = require_computed(series, self.name)
        return series.to_frame(name="value")

    def to_result(self, raw: pd.DataFrame, params: LengthParams) -> SingleValueResult:
        return series_to_single_value_result(self.name, params, raw["value"])
