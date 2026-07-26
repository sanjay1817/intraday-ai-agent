"""Unit tests for VWAP against real pandas-ta output."""

import pandas as pd
import pytest

from app.domain.exceptions.indicators import InvalidOHLCVDataError
from app.indicators.base import get_indicator_class
from app.indicators.engine import IndicatorEngine, IndicatorRequest


def test_vwap_has_no_warmup_period(ohlcv_df: pd.DataFrame) -> None:
    """Unlike SMA/EMA/RSI/etc., VWAP is defined from the first bar of a
    session — it must not have leading `None` values.
    """

    engine = IndicatorEngine()

    result = engine.calculate(ohlcv_df, [IndicatorRequest(name="VWAP")])["VWAP"]

    assert all(point.value is not None for point in result.values)


def test_vwap_first_bar_equals_typical_price(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    result = engine.calculate(ohlcv_df, [IndicatorRequest(name="VWAP")])["VWAP"]
    first_bar = ohlcv_df.iloc[0]
    expected_typical_price = (first_bar["high"] + first_bar["low"] + first_bar["close"]) / 3

    assert result.values[0].value == pytest.approx(expected_typical_price)


def test_vwap_stays_within_session_high_low_range(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    result = engine.calculate(ohlcv_df, [IndicatorRequest(name="VWAP")])["VWAP"]
    session_low, session_high = ohlcv_df["low"].min(), ohlcv_df["high"].max()

    assert all(session_low <= point.value <= session_high for point in result.values)


def test_vwap_indicator_rejects_non_datetime_index_directly(ohlcv_df: pd.DataFrame) -> None:
    """Exercises `VWAPIndicator.compute`'s own defensive check, bypassing
    `IndicatorEngine`'s validation (which would normally catch this first).
    """

    indicator_cls = get_indicator_class("VWAP")
    indicator = indicator_cls()
    broken = ohlcv_df.reset_index(drop=True)
    params = indicator_cls.params_model()

    with pytest.raises(InvalidOHLCVDataError):
        indicator.compute(broken, params)
