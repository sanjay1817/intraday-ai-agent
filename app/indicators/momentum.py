"""Momentum indicators: RSI, MACD, and Stochastic RSI."""

import pandas as pd
import pandas_ta as ta
from pydantic import Field

from app.indicators.base import Indicator, IndicatorParams, LengthParams, register_indicator
from app.indicators.schemas import (
    MACDPoint,
    MACDResult,
    SingleValuePoint,
    SingleValueResult,
    StochasticRSIPoint,
    StochasticRSIResult,
)
from app.indicators.utils import clean_float, require_computed, series_to_single_value_result


class RSIParams(LengthParams):
    """RSI conventionally defaults to a 14-period lookback, unlike the
    moving averages sharing `LengthParams`' generic default of 20.
    """

    length: int = Field(default=14, gt=0)


@register_indicator
class RSIIndicator(Indicator[RSIParams, SingleValuePoint]):
    """Relative Strength Index of `close`."""

    name = "RSI"
    params_model = RSIParams

    def compute(self, df: pd.DataFrame, params: RSIParams) -> pd.DataFrame:
        series = ta.rsi(df["close"], length=params.length, talib=False)
        series = require_computed(series, self.name)
        return series.to_frame(name="value")

    def to_result(self, raw: pd.DataFrame, params: RSIParams) -> SingleValueResult:
        return series_to_single_value_result(self.name, params, raw["value"])


class MACDParams(IndicatorParams):
    """Fast/slow EMA periods plus the signal-line smoothing period."""

    fast: int = Field(default=12, gt=0)
    slow: int = Field(default=26, gt=0)
    signal: int = Field(default=9, gt=0)


@register_indicator
class MACDIndicator(Indicator[MACDParams, MACDPoint]):
    """Moving Average Convergence Divergence of `close`."""

    name = "MACD"
    params_model = MACDParams

    def compute(self, df: pd.DataFrame, params: MACDParams) -> pd.DataFrame:
        raw = ta.macd(
            df["close"], fast=params.fast, slow=params.slow, signal=params.signal, talib=False
        )
        raw = require_computed(raw, self.name)
        # pandas-ta's macd() returns columns in this order: [macd, histogram, signal].
        raw.columns = ["macd", "histogram", "signal"]
        return raw

    def to_result(self, raw: pd.DataFrame, params: MACDParams) -> MACDResult:
        values = [
            MACDPoint(
                timestamp=row.Index,
                macd=clean_float(row.macd),
                histogram=clean_float(row.histogram),
                signal=clean_float(row.signal),
            )
            for row in raw.itertuples()
        ]
        return MACDResult(name=self.name, params=params.model_dump(), values=values)


class StochasticRSIParams(IndicatorParams):
    """Stochastic-oscillator-of-RSI periods: the RSI lookback, the
    stochastic lookback applied to it, and the %K/%D smoothing periods.
    """

    length: int = Field(default=14, gt=0)
    rsi_length: int = Field(default=14, gt=0)
    k: int = Field(default=3, gt=0)
    d: int = Field(default=3, gt=0)


@register_indicator
class StochasticRSIIndicator(Indicator[StochasticRSIParams, StochasticRSIPoint]):
    """Stochastic RSI of `close`."""

    name = "STOCH_RSI"
    params_model = StochasticRSIParams

    def compute(self, df: pd.DataFrame, params: StochasticRSIParams) -> pd.DataFrame:
        raw = ta.stochrsi(
            df["close"],
            length=params.length,
            rsi_length=params.rsi_length,
            k=params.k,
            d=params.d,
            talib=False,
        )
        raw = require_computed(raw, self.name)
        # pandas-ta's stochrsi() returns columns in this order: [%K, %D].
        raw.columns = ["k", "d"]
        return raw

    def to_result(self, raw: pd.DataFrame, params: StochasticRSIParams) -> StochasticRSIResult:
        values = [
            StochasticRSIPoint(timestamp=row.Index, k=clean_float(row.k), d=clean_float(row.d))
            for row in raw.itertuples()
        ]
        return StochasticRSIResult(name=self.name, params=params.model_dump(), values=values)
