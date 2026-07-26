"""Feature Engineering: turns `FeatureSpec`s into an actual feature matrix.

Given OHLCV candles and already-computed indicator results, builds one
pandas column per `FeatureSpec` — this module never computes an
indicator itself, that's `app.indicators.IndicatorEngine`'s job; it only
reshapes/transforms values that already exist. The resulting DataFrame
is what `dataset_manager.py` (next file) assembles into a labeled,
ML-ready dataset.

`PRICE_DERIVED`/`VOLUME_DERIVED` features compute a simple percentage
change of the source series bar-over-bar — the most common "derived
from price/volume" feature and the natural reading of `FeatureSpec`,
which has no separate parameter selecting a different transform (e.g.
log-returns). A rolling z-score of price is instead expressed as a
`ROLLING_STATISTIC` feature with `rolling_statistic=ZSCORE`, since that
is already a distinct, explicitly-parameterized feature type.
"""

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from app.domain.exceptions.research import FeatureEngineeringError, MissingFeatureSourceError
from app.indicators.schemas import IndicatorResult
from app.market.dto import MarketCandle
from app.research.dto import FeatureSpec, FeatureType, RollingStatistic

#: Raw OHLCV columns every feature matrix starts from. A `FeatureSpec`
#: whose `source_indicator` names one of these (and isn't itself a key
#: in the supplied `indicators` mapping) reads straight from `candles`
#: rather than looking up a computed indicator.
_RAW_OHLCV_COLUMNS = frozenset({"open", "high", "low", "close", "volume"})


def build_feature_matrix(
    candles: Sequence[MarketCandle],
    features: Sequence[FeatureSpec],
    indicators: Mapping[str, IndicatorResult[Any]] | None = None,
) -> pd.DataFrame:
    """Compute every `FeatureSpec` in `features` into one column of the
    returned DataFrame, indexed by candle timestamp.

    Args:
        candles: The OHLCV history every feature is ultimately derived
            from (directly, for price/volume features, or indirectly,
            for indicator-based ones).
        features: Which features to compute — only these, never every
            `FeatureSpec` a caller has ever defined.
        indicators: Already-computed indicator results, keyed by
            indicator name (matching `IndicatorRequest.result_key` from
            `app.indicators.IndicatorEngine.calculate`). Omit for a
            feature set built entirely from raw OHLCV columns.

    Raises:
        FeatureEngineeringError: `candles` is empty.
        MissingFeatureSourceError: a `FeatureSpec` references an
            indicator (or indicator field) not present in `indicators`.
    """

    if not candles:
        raise FeatureEngineeringError("cannot build a feature matrix from zero candles")

    resolved_indicators = indicators or {}
    index = pd.DatetimeIndex([candle.timestamp for candle in candles])
    ohlcv = pd.DataFrame(
        {
            "open": [candle.open for candle in candles],
            "high": [candle.high for candle in candles],
            "low": [candle.low for candle in candles],
            "close": [candle.close for candle in candles],
            "volume": [candle.volume for candle in candles],
        },
        index=index,
    )

    columns: dict[str, pd.Series] = {}
    for spec in features:
        source = _resolve_source_series(spec, ohlcv, resolved_indicators).reindex(index)
        columns[spec.name] = _apply_transform(spec, source)

    return pd.DataFrame(columns, index=index)


def _resolve_source_series(
    spec: FeatureSpec, ohlcv: pd.DataFrame, indicators: Mapping[str, IndicatorResult[Any]]
) -> pd.Series:
    """Resolve the raw series a feature is built from: a raw OHLCV
    column, or one field of an already-computed indicator's values.
    """

    name = spec.source_indicator
    if name is None:
        raise MissingFeatureSourceError(spec.name, requested="<none>", available=sorted(indicators))

    if name in _RAW_OHLCV_COLUMNS and name not in indicators:
        return ohlcv[name]

    if name not in indicators:
        raise MissingFeatureSourceError(spec.name, requested=name, available=sorted(indicators))

    indicator_result = indicators[name]
    if not indicator_result.values:
        raise MissingFeatureSourceError(
            spec.name, requested=f"{name}.{spec.source_field}", available=[]
        )

    point_fields = type(indicator_result.values[0]).model_fields
    if spec.source_field not in point_fields:
        raise MissingFeatureSourceError(
            spec.name,
            requested=f"{name}.{spec.source_field}",
            available=[f"{name}.{field}" for field in sorted(point_fields)],
        )

    timestamps = [point.timestamp for point in indicator_result.values]
    values = [getattr(point, spec.source_field) for point in indicator_result.values]
    return pd.Series(values, index=pd.DatetimeIndex(timestamps), name=name, dtype="float64")


def _apply_transform(spec: FeatureSpec, source: pd.Series) -> pd.Series:
    """Apply `spec.feature_type`'s transform to the already-resolved
    `source` series.
    """

    if spec.feature_type is FeatureType.INDICATOR:
        return source

    if spec.feature_type in (FeatureType.PRICE_DERIVED, FeatureType.VOLUME_DERIVED):
        return source.pct_change()

    if spec.feature_type is FeatureType.LAGGED:
        if spec.lag_periods is None:
            raise FeatureEngineeringError(f"feature {spec.name!r}: LAGGED requires lag_periods")
        return source.shift(spec.lag_periods)

    if spec.feature_type is FeatureType.ROLLING_STATISTIC:
        if spec.rolling_window is None or spec.rolling_statistic is None:
            raise FeatureEngineeringError(
                f"feature {spec.name!r}: ROLLING_STATISTIC requires "
                "rolling_window and rolling_statistic"
            )
        return _rolling_statistic(source, spec.rolling_window, spec.rolling_statistic)

    raise AssertionError(f"unhandled feature_type: {spec.feature_type}")  # pragma: no cover


def _rolling_statistic(source: pd.Series, window: int, statistic: RollingStatistic) -> pd.Series:
    """Compute one `RollingStatistic` over `source` with the given window."""

    rolling = source.rolling(window)

    if statistic is RollingStatistic.MEAN:
        return rolling.mean()
    if statistic is RollingStatistic.STD:
        return rolling.std()
    if statistic is RollingStatistic.MIN:
        return rolling.min()
    if statistic is RollingStatistic.MAX:
        return rolling.max()
    if statistic is RollingStatistic.SKEW:
        return rolling.skew()
    if statistic is RollingStatistic.KURTOSIS:
        return rolling.kurt()
    if statistic is RollingStatistic.ZSCORE:
        mean = rolling.mean()
        std = rolling.std()
        return (source - mean) / std

    raise AssertionError(f"unhandled RollingStatistic: {statistic}")  # pragma: no cover
