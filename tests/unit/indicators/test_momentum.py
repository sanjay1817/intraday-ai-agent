"""Unit tests for RSI, MACD, and Stochastic RSI against real pandas-ta output."""

import pandas as pd
import pytest

from app.indicators.engine import IndicatorEngine, IndicatorRequest


def test_rsi_stays_within_zero_to_hundred(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    result = engine.calculate(ohlcv_df, [IndicatorRequest(name="RSI", params={"length": 14})])[
        "RSI"
    ]
    values = [point.value for point in result.values if point.value is not None]

    assert values
    assert all(0.0 <= v <= 100.0 for v in values)


def test_rsi_default_length_is_fourteen(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    result = engine.calculate(ohlcv_df, [IndicatorRequest(name="RSI")])["RSI"]

    assert result.params["length"] == 14


def test_macd_histogram_equals_macd_minus_signal(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    result = engine.calculate(ohlcv_df, [IndicatorRequest(name="MACD")])["MACD"]

    checked_any = False
    for point in result.values:
        if point.macd is None or point.signal is None or point.histogram is None:
            continue
        checked_any = True
        assert point.histogram == pytest.approx(point.macd - point.signal, abs=1e-9)
    assert checked_any


def test_macd_custom_periods_are_reflected_in_params(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    result = engine.calculate(
        ohlcv_df, [IndicatorRequest(name="MACD", params={"fast": 5, "slow": 13, "signal": 3})]
    )["MACD"]

    assert result.params == {"fast": 5, "slow": 13, "signal": 3}


def test_stochastic_rsi_k_and_d_stay_within_zero_to_hundred(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    result = engine.calculate(ohlcv_df, [IndicatorRequest(name="STOCH_RSI")])["STOCH_RSI"]
    k_values = [p.k for p in result.values if p.k is not None]
    d_values = [p.d for p in result.values if p.d is not None]

    assert k_values and d_values
    assert all(0.0 <= v <= 100.0 for v in k_values)
    assert all(0.0 <= v <= 100.0 for v in d_values)


def test_only_requested_momentum_indicators_are_computed(ohlcv_df: pd.DataFrame) -> None:
    engine = IndicatorEngine()

    results = engine.calculate(ohlcv_df, [IndicatorRequest(name="RSI")])

    assert set(results) == {"RSI"}
