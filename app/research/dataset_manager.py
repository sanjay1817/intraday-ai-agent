"""Dataset Creation: assembles a `DatasetRequest` into a labeled,
ML-ready feature matrix, with content-hash-based caching so an identical
request against unchanged data isn't rebuilt.

Reuses `app.research.feature_engineering.build_feature_matrix` for the
feature columns; only the supervised-learning label (a forward return
over `DatasetRequest.label_horizon_bars`) and dataset-level bookkeeping
belong here. This module never downloads or fetches candle data itself
— consistent with every other engine in this project, a caller supplies
`candles`/`indicators` already assembled.
"""

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from app.domain.exceptions.research import FeatureEngineeringError
from app.indicators.schemas import IndicatorResult
from app.market.dto import MarketCandle
from app.research.dto import DatasetRequest
from app.research.feature_engineering import build_feature_matrix
from app.research.models import DatasetSummary
from app.utils.cache import LRUCache

#: Column name the forward-return supervised-learning label is stored
#: under in every dataset this module builds.
LABEL_COLUMN_NAME = "label_forward_return"

_DEFAULT_CACHE_SIZE = 32

#: A dataset's cache key: (request content, candle content fingerprint).
_CacheKey = tuple[str, str]


class DatasetManager:
    """Builds and caches labeled feature matrices for `DatasetRequest`s.

    Caching mirrors `app.indicators.IndicatorEngine`'s approach: a
    content-hash-based key, an `LRUCache` owned by one instance (not a
    module-level singleton) — hold one long-lived `DatasetManager` to
    actually benefit from it across calls.
    """

    def __init__(self, cache_max_size: int = _DEFAULT_CACHE_SIZE) -> None:
        self._cache: LRUCache[_CacheKey, tuple[pd.DataFrame, DatasetSummary]] = LRUCache(
            max_size=cache_max_size
        )

    def build(
        self,
        request: DatasetRequest,
        candles: Sequence[MarketCandle],
        indicators: Mapping[str, IndicatorResult[Any]] | None = None,
    ) -> tuple[pd.DataFrame, DatasetSummary]:
        """Build (or reuse a cached) labeled feature matrix for `request`.

        Returns the full DataFrame (feature columns plus
        `LABEL_COLUMN_NAME`) and a `DatasetSummary` describing it.

        Raises:
            FeatureEngineeringError: `candles` don't match `request`'s
                declared symbol/exchange/interval, or there are too few
                candles for `request.label_horizon_bars` to leave any
                labeled rows. Also propagates from
                `build_feature_matrix` for a malformed `FeatureSpec`.
        """

        _check_candles_match_request(request, candles)
        if request.label_horizon_bars >= len(candles):
            raise FeatureEngineeringError(
                f"label_horizon_bars={request.label_horizon_bars} leaves no labeled rows "
                f"for {len(candles)} candles"
            )

        cache_key: _CacheKey = (request.model_dump_json(), _fingerprint_candles(candles))
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        feature_matrix = build_feature_matrix(candles, request.features, indicators)
        labeled = _add_label(feature_matrix, candles, request.label_horizon_bars)

        summary = DatasetSummary(
            symbol=request.symbol,
            exchange=request.exchange,
            interval=request.interval,
            from_date=request.from_date,
            to_date=request.to_date,
            row_count=len(labeled),
            feature_names=[spec.name for spec in request.features],
            label_name=LABEL_COLUMN_NAME,
            missing_value_count=int(labeled.isna().sum().sum()),
            content_hash=_hash_dataframe(labeled),
        )

        result = (labeled, summary)
        self._cache.set(cache_key, result)
        return result

    def clear_cache(self) -> None:
        """Drop every cached dataset."""

        self._cache.clear()

    @property
    def cache_size(self) -> int:
        """How many datasets are currently cached."""

        return len(self._cache)


def _check_candles_match_request(request: DatasetRequest, candles: Sequence[MarketCandle]) -> None:
    """Guard against a caller assembling `candles` for the wrong
    symbol/exchange/interval and silently getting a mislabeled dataset.
    """

    if not candles:
        raise FeatureEngineeringError("cannot build a dataset from zero candles")

    if any(
        candle.symbol != request.symbol or candle.exchange != request.exchange for candle in candles
    ):
        raise FeatureEngineeringError(
            f"candles must all be for {request.symbol}/{request.exchange}, "
            "as declared by the DatasetRequest"
        )

    if any(candle.interval != request.interval for candle in candles):
        raise FeatureEngineeringError(
            f"candles must all be at the {request.interval} interval, "
            "as declared by the DatasetRequest"
        )


def _add_label(
    feature_matrix: pd.DataFrame, candles: Sequence[MarketCandle], horizon_bars: int
) -> pd.DataFrame:
    """Append the forward-return label:
    `(close[t + horizon_bars] - close[t]) / close[t]`.

    The last `horizon_bars` rows have no future close to compute a label
    from and are dropped — a labeled dataset with `NaN` labels at the
    tail would silently corrupt any downstream train/validation split.
    """

    closes = pd.Series([candle.close for candle in candles], index=feature_matrix.index)
    forward_close = closes.shift(-horizon_bars)
    label = (forward_close - closes) / closes

    labeled = feature_matrix.copy()
    labeled[LABEL_COLUMN_NAME] = label
    return labeled.iloc[:-horizon_bars]


def _fingerprint_candles(candles: Sequence[MarketCandle]) -> str:
    """A stable content fingerprint of `candles`, used as part of the
    dataset cache key so a mutated/replaced candle history never returns
    a stale cached dataset.
    """

    digest = hashlib.sha256()
    for candle in candles:
        digest.update(
            f"{candle.timestamp.isoformat()}|{candle.open}|{candle.high}|"
            f"{candle.low}|{candle.close}|{candle.volume}".encode()
        )
    return digest.hexdigest()


def _hash_dataframe(df: pd.DataFrame) -> str:
    """A stable content hash of a built dataset, for
    `DatasetSummary.content_hash` (dedup/integrity checks downstream).
    """

    return hashlib.sha256(
        pd.util.hash_pandas_object(df, index=True).to_numpy().tobytes()
    ).hexdigest()
