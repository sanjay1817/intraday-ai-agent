"""Structured result models the Technical Indicator Engine returns.

`IndicatorResult` is generic over its point type so every indicator
returns a strongly-typed `values: list[...]`, but indicators whose output
shape is identical (one number per bar: SMA, EMA, RSI, ATR, VWAP, Volume
SMA) share `SingleValuePoint` instead of each inventing an equivalent
one-field model. Indicators with a genuinely different shape (MACD,
Bollinger Bands, ADX, SuperTrend, Stochastic RSI, Ichimoku) get their own
point model.

`value`/field members are `float | None` rather than `float` because
every indicator has a warm-up period (e.g. a 20-period SMA has no value
for its first 19 bars) where pandas-ta produces `NaN`; `None` is the
JSON-safe, Pydantic-native way to represent "not yet available" instead
of a non-serializable `NaN`.
"""

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict

PointT = TypeVar("PointT", bound=BaseModel)


class IndicatorResult(BaseModel, Generic[PointT]):
    """Uniform envelope every indicator returns.

    `values` is aligned 1:1 with the input DataFrame's rows, in the same
    order. `params` holds the fully-resolved parameters (explicit
    overrides merged with defaults) the calculation actually ran with.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    params: dict[str, Any]
    values: list[PointT]


class SingleValuePoint(BaseModel):
    """One numeric value per bar: SMA, EMA, RSI, ATR, VWAP, Volume SMA."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    value: float | None


class MACDPoint(BaseModel):
    """One bar of MACD: the MACD line, its signal line, and the histogram
    (`macd - signal`).
    """

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    macd: float | None
    signal: float | None
    histogram: float | None


class BollingerBandsPoint(BaseModel):
    """One bar of Bollinger Bands: lower/middle/upper bands, bandwidth
    (`100 * (upper - lower) / middle`), and %B (`close`'s position
    within the bands, 0 = at the lower band, 1 = at the upper band).
    """

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    lower: float | None
    middle: float | None
    upper: float | None
    bandwidth: float | None
    percent_b: float | None


class ADXPoint(BaseModel):
    """One bar of ADX: trend strength (`adx`), its smoothed/lagged
    variant (`adxr`), and the two directional indicators (`plus_di`,
    `minus_di`) ADX is derived from.
    """

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    adx: float | None
    adxr: float | None
    plus_di: float | None
    minus_di: float | None


class SuperTrendPoint(BaseModel):
    """One bar of SuperTrend: the active trend line's value, its
    direction (`1` = uptrend, `-1` = downtrend), and whichever of
    `long_band`/`short_band` is currently active (the other is `None`).
    """

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    value: float | None
    direction: int | None
    long_band: float | None
    short_band: float | None


class StochasticRSIPoint(BaseModel):
    """One bar of Stochastic RSI: the fast (`k`) and slow (`d`) lines."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    k: float | None
    d: float | None


class IchimokuPoint(BaseModel):
    """One bar of the Ichimoku Cloud's historical (non-forward-projected)
    lines. The forward-projected cloud (pandas-ta's second return value,
    extending `kijun` bars beyond the input's last timestamp) is out of
    scope: every other indicator in this engine returns one point per
    *input* bar, and synthetic future timestamps would break that
    contract.
    """

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    tenkan_sen: float | None
    kijun_sen: float | None
    senkou_span_a: float | None
    senkou_span_b: float | None
    chikou_span: float | None


SingleValueResult = IndicatorResult[SingleValuePoint]
MACDResult = IndicatorResult[MACDPoint]
BollingerBandsResult = IndicatorResult[BollingerBandsPoint]
ADXResult = IndicatorResult[ADXPoint]
SuperTrendResult = IndicatorResult[SuperTrendPoint]
StochasticRSIResult = IndicatorResult[StochasticRSIPoint]
IchimokuResult = IndicatorResult[IchimokuPoint]
