"""The Technical Indicator Engine.

`IndicatorEngine.calculate` computes only the indicators a caller asks
for — never the full pandas-ta surface — against one OHLCV DataFrame,
and caches each (DataFrame content, indicator, resolved params)
combination so re-requesting it is O(1) after the first call.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from pandas.util import hash_pandas_object

from app.domain.exceptions.indicators import InvalidOHLCVDataError
from app.indicators.base import get_indicator_class
from app.indicators.schemas import IndicatorResult
from app.utils.cache import LRUCache

_REQUIRED_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")
_DEFAULT_CACHE_SIZE = 256

# A cache key is (indicator name, sorted resolved-params items, DataFrame
# fingerprint) — see `_fingerprint_ohlcv` and `_freeze_params` below.
_CacheKey = tuple[str, tuple[tuple[str, Any], ...], int]


@dataclass(frozen=True, slots=True)
class IndicatorRequest:
    """One requested indicator plus its parameter overrides.

    `alias` distinguishes multiple parameterizations of the same
    indicator requested together (e.g. a fast and a slow EMA); it
    defaults to `name` and becomes the key in `IndicatorEngine.calculate`'s
    result dict.
    """

    name: str
    params: dict[str, Any] = field(default_factory=dict)
    alias: str | None = None

    @property
    def result_key(self) -> str:
        """The key this request's result is stored under."""

        return self.alias or self.name


class IndicatorEngine:
    """Computes technical indicators against an OHLCV DataFrame.

    Each instance owns its own cache; hold one long-lived instance (e.g.
    as a FastAPI dependency singleton) to actually benefit from caching
    across calls — a fresh `IndicatorEngine()` per call has nothing to
    reuse.
    """

    def __init__(self, cache_max_size: int = _DEFAULT_CACHE_SIZE) -> None:
        self._cache: LRUCache[_CacheKey, IndicatorResult[Any]] = LRUCache(max_size=cache_max_size)

    def calculate(
        self, df: pd.DataFrame, requests: Sequence[IndicatorRequest]
    ) -> dict[str, IndicatorResult[Any]]:
        """Compute every indicator in `requests` against `df`.

        Returns a dict keyed by each request's `result_key`. Raises
        `InvalidOHLCVDataError` if `df` isn't valid OHLCV data, or
        `UnknownIndicatorError` if a request names an unregistered
        indicator (both propagate from their respective helpers).
        """

        _validate_ohlcv(df)
        fingerprint = _fingerprint_ohlcv(df)

        results: dict[str, IndicatorResult[Any]] = {}
        for request in requests:
            indicator_cls = get_indicator_class(request.name)
            validated_params = indicator_cls.params_model.model_validate(request.params)
            cache_key: _CacheKey = (
                request.name,
                _freeze_params(validated_params.model_dump()),
                fingerprint,
            )

            cached = self._cache.get(cache_key)
            if cached is None:
                indicator = indicator_cls()
                raw = indicator.compute(df, validated_params)
                cached = indicator.to_result(raw, validated_params)
                self._cache.set(cache_key, cached)

            results[request.result_key] = cached

        return results

    def clear_cache(self) -> None:
        """Drop every cached result."""

        self._cache.clear()

    @property
    def cache_size(self) -> int:
        """How many (indicator, params, data) combinations are cached."""

        return len(self._cache)


def _validate_ohlcv(df: pd.DataFrame) -> None:
    """Raise `InvalidOHLCVDataError` if `df` isn't usable OHLCV data."""

    if df.empty:
        raise InvalidOHLCVDataError("OHLCV DataFrame is empty")

    missing = [column for column in _REQUIRED_OHLCV_COLUMNS if column not in df.columns]
    if missing:
        raise InvalidOHLCVDataError(f"OHLCV DataFrame is missing required columns: {missing}")

    if not isinstance(df.index, pd.DatetimeIndex):
        raise InvalidOHLCVDataError(
            "OHLCV DataFrame must be indexed by a pandas DatetimeIndex "
            "(required by session-anchored indicators such as VWAP)"
        )

    if not df.index.is_monotonic_increasing:
        raise InvalidOHLCVDataError("OHLCV DataFrame index must be sorted in ascending order")


def _fingerprint_ohlcv(df: pd.DataFrame) -> int:
    """A content-sensitive fingerprint of the OHLCV columns, used as part
    of the cache key so a mutated or replaced DataFrame never returns a
    stale cached result.

    Uses pandas' vectorized row hashing (`hash_pandas_object`) rather than
    a Python-level loop, so this stays O(n) — no worse than the O(n)
    indicator computation it protects — instead of the cheaper but
    collision-prone alternative of hashing only shape/first/last values.
    """

    subset = df.loc[:, list(_REQUIRED_OHLCV_COLUMNS)]
    return int(hash_pandas_object(subset, index=True).sum())


def _freeze_params(params: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    """Convert a resolved-params dict into a hashable, order-independent
    tuple suitable for use in a cache key.
    """

    return tuple(sorted(params.items()))
