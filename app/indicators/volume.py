"""Volume-based indicator: VWAP."""

import pandas as pd
import pandas_ta as ta

from app.domain.exceptions.indicators import InvalidOHLCVDataError
from app.indicators.base import Indicator, IndicatorParams, register_indicator
from app.indicators.schemas import SingleValuePoint, SingleValueResult
from app.indicators.utils import series_to_single_value_result

#: VWAP always resets at the start of each calendar day — the standard
#: convention for intraday trading — so there is nothing to parameterize.
_DAILY_ANCHOR = "D"


class NoParams(IndicatorParams):
    """VWAP has no tunable parameters in this engine."""


@register_indicator
class VWAPIndicator(Indicator[NoParams, SingleValuePoint]):
    """Volume Weighted Average Price, anchored to the calendar day.

    Unlike every other indicator here, VWAP has no warm-up period: its
    first value is defined from the first bar of each session.
    """

    name = "VWAP"
    params_model = NoParams

    def compute(self, df: pd.DataFrame, params: NoParams) -> pd.DataFrame:
        series = ta.vwap(df["high"], df["low"], df["close"], df["volume"], anchor=_DAILY_ANCHOR)
        if series is None:
            # pandas-ta signals "no ordered DatetimeIndex" by returning
            # None (and printing a warning) rather than raising; convert
            # that into a proper exception. `IndicatorEngine` already
            # validates this before dispatch, so this only triggers when
            # an indicator is used directly, bypassing the engine.
            raise InvalidOHLCVDataError(
                "VWAP requires an ordered pandas DatetimeIndex on the OHLCV DataFrame"
            )
        return series.to_frame(name="value")

    def to_result(self, raw: pd.DataFrame, params: NoParams) -> SingleValueResult:
        return series_to_single_value_result(self.name, params, raw["value"])
