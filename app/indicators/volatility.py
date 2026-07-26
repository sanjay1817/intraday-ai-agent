"""Volatility indicators: ATR and Bollinger Bands."""

import pandas as pd
import pandas_ta as ta
from pydantic import Field

from app.indicators.base import Indicator, IndicatorParams, LengthParams, register_indicator
from app.indicators.schemas import (
    BollingerBandsPoint,
    BollingerBandsResult,
    SingleValuePoint,
    SingleValueResult,
)
from app.indicators.utils import clean_float, require_computed, series_to_single_value_result


class ATRParams(LengthParams):
    """ATR conventionally defaults to a 14-period lookback, unlike the
    moving averages sharing `LengthParams`' generic default of 20.
    """

    length: int = Field(default=14, gt=0)


@register_indicator
class ATRIndicator(Indicator[ATRParams, SingleValuePoint]):
    """Average True Range: volatility quantified via gaps/limit moves."""

    name = "ATR"
    params_model = ATRParams

    def compute(self, df: pd.DataFrame, params: ATRParams) -> pd.DataFrame:
        series = ta.atr(df["high"], df["low"], df["close"], length=params.length, talib=False)
        series = require_computed(series, self.name)
        return series.to_frame(name="value")

    def to_result(self, raw: pd.DataFrame, params: ATRParams) -> SingleValueResult:
        return series_to_single_value_result(self.name, params, raw["value"])


class BollingerBandsParams(IndicatorParams):
    """Moving-average period and the (symmetric) standard-deviation
    multiplier for both bands.
    """

    length: int = Field(default=20, gt=0)
    std: float = Field(default=2.0, gt=0)


@register_indicator
class BollingerBandsIndicator(Indicator[BollingerBandsParams, BollingerBandsPoint]):
    """Bollinger Bands: a moving average with volatility-scaled bands
    around it.
    """

    name = "BOLLINGER_BANDS"
    params_model = BollingerBandsParams

    def compute(self, df: pd.DataFrame, params: BollingerBandsParams) -> pd.DataFrame:
        raw = ta.bbands(
            df["close"],
            length=params.length,
            lower_std=params.std,
            upper_std=params.std,
            talib=False,
        )
        raw = require_computed(raw, self.name)
        # pandas-ta's bbands() returns columns in this order:
        # [lower, mid, upper, bandwidth, percent].
        raw.columns = ["lower", "middle", "upper", "bandwidth", "percent_b"]
        return raw

    def to_result(self, raw: pd.DataFrame, params: BollingerBandsParams) -> BollingerBandsResult:
        values = [
            BollingerBandsPoint(
                timestamp=row.Index,
                lower=clean_float(row.lower),
                middle=clean_float(row.middle),
                upper=clean_float(row.upper),
                bandwidth=clean_float(row.bandwidth),
                percent_b=clean_float(row.percent_b),
            )
            for row in raw.itertuples()
        ]
        return BollingerBandsResult(name=self.name, params=params.model_dump(), values=values)
