"""Unit tests for ADX, SuperTrend, and Ichimoku against real pandas-ta output."""

import pandas as pd

from app.indicators.engine import IndicatorEngine, IndicatorRequest


def test_adx_and_di_stay_non_negative(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    result = engine.calculate(ohlcv_df, [IndicatorRequest(name="ADX")])["ADX"]

    checked_any = False
    for point in result.values:
        if point.adx is None:
            continue
        checked_any = True
        assert point.adx >= 0.0
        assert point.plus_di is not None and point.plus_di >= 0.0
        assert point.minus_di is not None and point.minus_di >= 0.0
    assert checked_any


def test_supertrend_direction_is_always_plus_or_minus_one(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    result = engine.calculate(ohlcv_df, [IndicatorRequest(name="SUPERTREND")])["SUPERTREND"]
    directions = [point.direction for point in result.values if point.direction is not None]

    assert directions
    assert all(direction in (1, -1) for direction in directions)


def test_supertrend_exactly_one_band_active_per_direction(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    result = engine.calculate(ohlcv_df, [IndicatorRequest(name="SUPERTREND")])["SUPERTREND"]

    checked_any = False
    for point in result.values:
        if point.direction is None:
            continue
        checked_any = True
        if point.direction == 1:
            assert point.long_band is not None
            assert point.short_band is None
        else:
            assert point.short_band is not None
            assert point.long_band is None
    assert checked_any


def test_ichimoku_lines_present_after_warmup(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    result = engine.calculate(
        ohlcv_df,
        [IndicatorRequest(name="ICHIMOKU", params={"tenkan": 9, "kijun": 26, "senkou": 52})],
    )["ICHIMOKU"]
    last = result.values[-1]

    assert last.tenkan_sen is not None
    assert last.kijun_sen is not None
    assert last.senkou_span_a is not None
    assert last.senkou_span_b is not None


def test_ichimoku_result_length_matches_input(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    result = engine.calculate(ohlcv_df, [IndicatorRequest(name="ICHIMOKU")])["ICHIMOKU"]

    assert len(result.values) == len(ohlcv_df)
    assert result.values[0].timestamp == ohlcv_df.index[0]
    assert result.values[-1].timestamp == ohlcv_df.index[-1]
