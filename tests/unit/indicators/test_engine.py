"""Unit tests for `IndicatorEngine` mechanics: registry lookup,
only-requested computation, caching, aliasing, and OHLCV validation.

These register a fake indicator ad hoc (not one of the real 12) so the
tests exercise engine mechanics independent of any real indicator's
pandas-ta internals — and, not incidentally, demonstrate the Open/Closed
Principle: this file never touches `app/indicators/base.py` or any real
indicator module to add "a new indicator" for testing purposes.
"""

from collections.abc import Iterator

import pandas as pd
import pytest

from app.domain.exceptions.indicators import InvalidOHLCVDataError, UnknownIndicatorError
from app.indicators import base as indicator_base
from app.indicators.base import Indicator, IndicatorParams, register_indicator
from app.indicators.engine import IndicatorEngine, IndicatorRequest
from app.indicators.schemas import IndicatorResult, SingleValuePoint

_TEST_INDICATOR_NAME = "TEST_COUNTER"


class _CountingParams(IndicatorParams):
    length: int = 3


class _CountingIndicator(Indicator[_CountingParams, SingleValuePoint]):
    """A fake indicator that counts real `compute` invocations, so tests
    can assert a cache hit skipped computation entirely.
    """

    name = _TEST_INDICATOR_NAME
    params_model = _CountingParams

    call_count = 0

    def compute(self, df: pd.DataFrame, params: _CountingParams) -> pd.DataFrame:
        type(self).call_count += 1
        return pd.DataFrame({"value": df["close"] * params.length}, index=df.index)

    def to_result(
        self, raw: pd.DataFrame, params: _CountingParams
    ) -> IndicatorResult[SingleValuePoint]:
        values = [SingleValuePoint(timestamp=ts, value=float(v)) for ts, v in raw["value"].items()]
        return IndicatorResult(name=self.name, params=params.model_dump(), values=values)


@pytest.fixture(autouse=True)
def _register_test_indicator() -> Iterator[None]:
    _CountingIndicator.call_count = 0
    register_indicator(_CountingIndicator)
    try:
        yield
    finally:
        del indicator_base._REGISTRY[_TEST_INDICATOR_NAME]


def test_calculates_only_requested_indicators(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    results = engine.calculate(ohlcv_df, [IndicatorRequest(name=_TEST_INDICATOR_NAME)])

    assert set(results) == {_TEST_INDICATOR_NAME}
    assert _CountingIndicator.call_count == 1


def test_repeated_identical_request_hits_cache(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    engine.calculate(ohlcv_df, [IndicatorRequest(name=_TEST_INDICATOR_NAME)])
    engine.calculate(ohlcv_df, [IndicatorRequest(name=_TEST_INDICATOR_NAME)])

    assert _CountingIndicator.call_count == 1


def test_different_params_bypass_cache(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    engine.calculate(ohlcv_df, [IndicatorRequest(name=_TEST_INDICATOR_NAME, params={"length": 3})])
    engine.calculate(ohlcv_df, [IndicatorRequest(name=_TEST_INDICATOR_NAME, params={"length": 5})])

    assert _CountingIndicator.call_count == 2


def test_explicit_default_value_still_hits_cache(ohlcv_df: pd.DataFrame) -> None:
    """Requesting with `params={}` and with `params={"length": 3}`
    (the model's default) must resolve to the same cache entry, since
    both validate to identical resolved parameters.
    """

    engine = IndicatorEngine()

    engine.calculate(ohlcv_df, [IndicatorRequest(name=_TEST_INDICATOR_NAME)])
    engine.calculate(ohlcv_df, [IndicatorRequest(name=_TEST_INDICATOR_NAME, params={"length": 3})])

    assert _CountingIndicator.call_count == 1


def test_different_dataframe_bypasses_cache(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    engine.calculate(ohlcv_df, [IndicatorRequest(name=_TEST_INDICATOR_NAME)])
    mutated = ohlcv_df.copy()
    mutated.iloc[0, mutated.columns.get_loc("close")] += 1.0
    engine.calculate(mutated, [IndicatorRequest(name=_TEST_INDICATOR_NAME)])

    assert _CountingIndicator.call_count == 2


def test_multiple_aliases_of_same_indicator(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    results = engine.calculate(
        ohlcv_df,
        [
            IndicatorRequest(name=_TEST_INDICATOR_NAME, params={"length": 3}, alias="fast"),
            IndicatorRequest(name=_TEST_INDICATOR_NAME, params={"length": 9}, alias="slow"),
        ],
    )

    assert set(results) == {"fast", "slow"}
    assert results["fast"].values[-1].value != results["slow"].values[-1].value
    assert _CountingIndicator.call_count == 2


def test_unknown_indicator_raises(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    with pytest.raises(UnknownIndicatorError):
        engine.calculate(ohlcv_df, [IndicatorRequest(name="NOT_REGISTERED")])


def test_missing_columns_raises(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()
    broken = ohlcv_df.drop(columns=["volume"])

    with pytest.raises(InvalidOHLCVDataError):
        engine.calculate(broken, [IndicatorRequest(name=_TEST_INDICATOR_NAME)])


def test_non_datetime_index_raises(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()
    broken = ohlcv_df.reset_index(drop=True)

    with pytest.raises(InvalidOHLCVDataError):
        engine.calculate(broken, [IndicatorRequest(name=_TEST_INDICATOR_NAME)])


def test_unsorted_index_raises(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()
    shuffled = ohlcv_df.iloc[::-1]

    with pytest.raises(InvalidOHLCVDataError):
        engine.calculate(shuffled, [IndicatorRequest(name=_TEST_INDICATOR_NAME)])


def test_empty_dataframe_raises(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    with pytest.raises(InvalidOHLCVDataError):
        engine.calculate(ohlcv_df.iloc[0:0], [IndicatorRequest(name=_TEST_INDICATOR_NAME)])


def test_clear_cache_forces_recompute(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    engine.calculate(ohlcv_df, [IndicatorRequest(name=_TEST_INDICATOR_NAME)])
    assert engine.cache_size == 1

    engine.clear_cache()
    assert engine.cache_size == 0

    engine.calculate(ohlcv_df, [IndicatorRequest(name=_TEST_INDICATOR_NAME)])
    assert _CountingIndicator.call_count == 2


def test_cache_eviction_respects_max_size(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine(cache_max_size=1)

    engine.calculate(ohlcv_df, [IndicatorRequest(name=_TEST_INDICATOR_NAME, params={"length": 1})])
    engine.calculate(ohlcv_df, [IndicatorRequest(name=_TEST_INDICATOR_NAME, params={"length": 2})])

    assert engine.cache_size == 1
    # The length=1 entry was evicted, so recomputing it is a fresh call.
    engine.calculate(ohlcv_df, [IndicatorRequest(name=_TEST_INDICATOR_NAME, params={"length": 1})])
    assert _CountingIndicator.call_count == 3
