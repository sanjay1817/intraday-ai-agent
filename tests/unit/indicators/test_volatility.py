"""Unit tests for ATR and Bollinger Bands against real pandas-ta output."""

import pandas as pd
import pytest

from app.indicators.engine import IndicatorEngine, IndicatorRequest


def test_atr_is_positive_after_warmup(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    result = engine.calculate(ohlcv_df, [IndicatorRequest(name="ATR", params={"length": 14})])[
        "ATR"
    ]
    values = [point.value for point in result.values if point.value is not None]

    assert values
    assert all(v > 0 for v in values)


def test_bollinger_bands_ordering_holds(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    result = engine.calculate(ohlcv_df, [IndicatorRequest(name="BOLLINGER_BANDS")])[
        "BOLLINGER_BANDS"
    ]

    checked_any = False
    for point in result.values:
        if point.lower is None or point.middle is None or point.upper is None:
            continue
        checked_any = True
        assert point.lower <= point.middle <= point.upper
    assert checked_any


def test_bollinger_bands_percent_b_reflects_close_position(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    result = engine.calculate(
        ohlcv_df, [IndicatorRequest(name="BOLLINGER_BANDS", params={"length": 20, "std": 2.0})]
    )["BOLLINGER_BANDS"]
    last = result.values[-1]
    close = ohlcv_df["close"].iloc[-1]

    assert last.lower is not None and last.upper is not None and last.percent_b is not None
    expected_percent_b = (close - last.lower) / (last.upper - last.lower)
    assert last.percent_b == pytest.approx(expected_percent_b, abs=1e-6)


def test_bollinger_bands_custom_std_widens_bands(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    narrow = engine.calculate(
        ohlcv_df, [IndicatorRequest(name="BOLLINGER_BANDS", params={"std": 1.0}, alias="narrow")]
    )["narrow"]
    wide = engine.calculate(
        ohlcv_df, [IndicatorRequest(name="BOLLINGER_BANDS", params={"std": 3.0}, alias="wide")]
    )["wide"]

    narrow_last, wide_last = narrow.values[-1], wide.values[-1]
    assert narrow_last.upper is not None and wide_last.upper is not None
    assert narrow_last.lower is not None and wide_last.lower is not None
    assert (wide_last.upper - wide_last.lower) > (narrow_last.upper - narrow_last.lower)
