"""Trend indicators: ADX, SuperTrend, and the Ichimoku Cloud."""

import pandas as pd
import pandas_ta as ta
from pydantic import Field

from app.indicators.base import Indicator, IndicatorParams, LengthParams, register_indicator
from app.indicators.schemas import (
    ADXPoint,
    ADXResult,
    IchimokuPoint,
    IchimokuResult,
    SuperTrendPoint,
    SuperTrendResult,
)
from app.indicators.utils import clean_float, require_computed


@register_indicator
class ADXIndicator(Indicator[LengthParams, ADXPoint]):
    """Average Directional Movement Index: trend-strength (not direction)."""

    name = "ADX"
    params_model = LengthParams

    def compute(self, df: pd.DataFrame, params: LengthParams) -> pd.DataFrame:
        raw = ta.adx(df["high"], df["low"], df["close"], length=params.length, talib=False)
        raw = require_computed(raw, self.name)
        # pandas-ta's adx() returns columns in this order:
        # [ADX, ADXR, +DI (DMP), -DI (DMN)].
        raw.columns = ["adx", "adxr", "plus_di", "minus_di"]
        return raw

    def to_result(self, raw: pd.DataFrame, params: LengthParams) -> ADXResult:
        values = [
            ADXPoint(
                timestamp=row.Index,
                adx=clean_float(row.adx),
                adxr=clean_float(row.adxr),
                plus_di=clean_float(row.plus_di),
                minus_di=clean_float(row.minus_di),
            )
            for row in raw.itertuples()
        ]
        return ADXResult(name=self.name, params=params.model_dump(), values=values)


class SuperTrendParams(IndicatorParams):
    """ATR lookback period and the band-distance multiplier."""

    length: int = Field(default=7, gt=0)
    multiplier: float = Field(default=3.0, gt=0)


@register_indicator
class SuperTrendIndicator(Indicator[SuperTrendParams, SuperTrendPoint]):
    """SuperTrend: an ATR-banded trend-following overlay."""

    name = "SUPERTREND"
    params_model = SuperTrendParams

    def compute(self, df: pd.DataFrame, params: SuperTrendParams) -> pd.DataFrame:
        raw = ta.supertrend(
            df["high"], df["low"], df["close"], length=params.length, multiplier=params.multiplier
        )
        raw = require_computed(raw, self.name)
        # pandas-ta's supertrend() returns columns in this order:
        # [trend value, direction, long band, short band].
        raw.columns = ["value", "direction", "long_band", "short_band"]
        return raw

    def to_result(self, raw: pd.DataFrame, params: SuperTrendParams) -> SuperTrendResult:
        values = [
            SuperTrendPoint(
                timestamp=row.Index,
                value=clean_float(row.value),
                direction=_clean_direction(row.direction),
                long_band=clean_float(row.long_band),
                short_band=clean_float(row.short_band),
            )
            for row in raw.itertuples()
        ]
        return SuperTrendResult(name=self.name, params=params.model_dump(), values=values)


def _clean_direction(value: object) -> int | None:
    """SuperTrend's direction column is `NaN` during warm-up and `1`/`-1`
    (as a float) once established; convert to a plain `int` or `None`.
    """

    cleaned = clean_float(value)
    return None if cleaned is None else int(cleaned)


class IchimokuParams(IndicatorParams):
    """The three classic Ichimoku lookback periods."""

    tenkan: int = Field(default=9, gt=0)
    kijun: int = Field(default=26, gt=0)
    senkou: int = Field(default=52, gt=0)


@register_indicator
class IchimokuIndicator(Indicator[IchimokuParams, IchimokuPoint]):
    """Ichimoku Kinkō Hyō: a multi-line trend/support-resistance system.

    Only the historical (input-timestamp-aligned) lines are returned —
    see `IchimokuPoint`'s docstring for why the forward-projected cloud
    is intentionally out of scope.
    """

    name = "ICHIMOKU"
    params_model = IchimokuParams

    def compute(self, df: pd.DataFrame, params: IchimokuParams) -> pd.DataFrame:
        historical, _forward_cloud = ta.ichimoku(
            df["high"],
            df["low"],
            df["close"],
            tenkan=params.tenkan,
            kijun=params.kijun,
            senkou=params.senkou,
        )
        historical = require_computed(historical, self.name)
        # pandas-ta's ichimoku() historical frame column order:
        # [senkou span A (ISA), senkou span B (ISB), tenkan-sen (ITS),
        #  kijun-sen (IKS), chikou span (ICS)].
        historical.columns = [
            "senkou_span_a",
            "senkou_span_b",
            "tenkan_sen",
            "kijun_sen",
            "chikou_span",
        ]
        return historical

    def to_result(self, raw: pd.DataFrame, params: IchimokuParams) -> IchimokuResult:
        values = [
            IchimokuPoint(
                timestamp=row.Index,
                tenkan_sen=clean_float(row.tenkan_sen),
                kijun_sen=clean_float(row.kijun_sen),
                senkou_span_a=clean_float(row.senkou_span_a),
                senkou_span_b=clean_float(row.senkou_span_b),
                chikou_span=clean_float(row.chikou_span),
            )
            for row in raw.itertuples()
        ]
        return IchimokuResult(name=self.name, params=params.model_dump(), values=values)
