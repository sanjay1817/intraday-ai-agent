"""Unit tests for SMA, EMA, and Volume SMA against real pandas-ta output."""

import pandas as pd
import pytest
from pydantic import ValidationError

from app.indicators.engine import IndicatorEngine, IndicatorRequest


def test_sma_matches_pandas_rolling_mean(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    result = engine.calculate(ohlcv_df, [IndicatorRequest(name="SMA", params={"length": 10})])[
        "SMA"
    ]
    expected = ohlcv_df["close"].rolling(10).mean()

    assert len(result.values) == len(ohlcv_df)
    for point, (timestamp, expected_value) in zip(result.values, expected.items(), strict=True):
        assert point.timestamp == timestamp
        if pd.isna(expected_value):
            assert point.value is None
        else:
            assert point.value == pytest.approx(expected_value)


def test_sma_default_length_is_twenty(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    result = engine.calculate(ohlcv_df, [IndicatorRequest(name="SMA")])["SMA"]

    assert result.params["length"] == 20
    assert all(point.value is None for point in result.values[:19])
    assert result.values[19].value is not None


def test_ema_has_no_nulls_after_warmup(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    result = engine.calculate(ohlcv_df, [IndicatorRequest(name="EMA", params={"length": 20})])[
        "EMA"
    ]

    assert all(point.value is not None for point in result.values[-20:])


def test_volume_sma_uses_volume_column_not_close(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    result = engine.calculate(
        ohlcv_df, [IndicatorRequest(name="VOLUME_SMA", params={"length": 5})]
    )["VOLUME_SMA"]
    expected = ohlcv_df["volume"].rolling(5).mean()

    assert result.values[-1].value == pytest.approx(expected.iloc[-1])
    assert result.values[-1].value != pytest.approx(ohlcv_df["close"].rolling(5).mean().iloc[-1])


def test_invalid_length_is_rejected(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    with pytest.raises(ValidationError, match="length"):
        engine.calculate(ohlcv_df, [IndicatorRequest(name="SMA", params={"length": 0})])
