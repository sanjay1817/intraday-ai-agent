"""Unit tests for `app.market.indicator_runtime`."""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.enums.trading import Exchange, HistoricalInterval
from app.domain.exceptions.indicators import InvalidOHLCVDataError, UnknownIndicatorError
from app.indicators.engine import IndicatorEngine, IndicatorRequest
from app.market.dto import MarketCandle
from app.market.indicator_runtime import compute_indicators


def _candles(closes: list[float]) -> list[MarketCandle]:
    start = datetime(2024, 1, 1, 9, 15, tzinfo=UTC)
    return [
        MarketCandle(
            symbol="TEST",
            exchange=Exchange.NSE,
            interval=HistoricalInterval.FIVE_MINUTE,
            timestamp=start + timedelta(minutes=5 * i),
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=1000 + i,
        )
        for i, close in enumerate(closes)
    ]


def test_compute_indicators_matches_hand_computed_sma() -> None:
    # A 3-period SMA over [100, 101, 102, 103, 104] ends at (102+103+104)/3 = 103.0.
    candles = _candles([100.0, 101.0, 102.0, 103.0, 104.0])
    engine = IndicatorEngine()

    results = compute_indicators(
        engine, candles, [IndicatorRequest(name="SMA", params={"length": 3})]
    )

    sma = results["SMA"]
    assert sma.values[-1].value == pytest.approx(103.0)
    assert sma.values[0].value is None  # warm-up period


def test_compute_indicators_preserves_candle_order_and_count() -> None:
    candles = _candles([100.0, 101.0, 102.0, 103.0, 104.0])
    engine = IndicatorEngine()

    results = compute_indicators(
        engine, candles, [IndicatorRequest(name="SMA", params={"length": 2})]
    )

    assert len(results["SMA"].values) == len(candles)
    assert [point.timestamp for point in results["SMA"].values] == [
        candle.timestamp for candle in candles
    ]


def test_compute_indicators_runs_multiple_aliased_requests_together() -> None:
    candles = _candles([100.0 + i for i in range(20)])
    engine = IndicatorEngine()

    results = compute_indicators(
        engine,
        candles,
        [
            IndicatorRequest(name="EMA", params={"length": 3}, alias="EMA_FAST"),
            IndicatorRequest(name="EMA", params={"length": 10}, alias="EMA_SLOW"),
            IndicatorRequest(name="RSI", params={"length": 5}),
        ],
    )

    assert set(results) == {"EMA_FAST", "EMA_SLOW", "RSI"}
    assert results["EMA_FAST"].values[-1].value is not None
    assert results["EMA_SLOW"].values[-1].value is not None


def test_compute_indicators_raises_on_empty_candles() -> None:
    engine = IndicatorEngine()

    with pytest.raises(InvalidOHLCVDataError):
        compute_indicators(engine, [], [IndicatorRequest(name="SMA", params={"length": 3})])


def test_compute_indicators_raises_on_unknown_indicator() -> None:
    candles = _candles([100.0, 101.0, 102.0])
    engine = IndicatorEngine()

    with pytest.raises(UnknownIndicatorError):
        compute_indicators(engine, candles, [IndicatorRequest(name="NOT_A_REAL_INDICATOR")])


def test_compute_indicators_reuses_engine_cache_across_calls() -> None:
    candles = _candles([100.0, 101.0, 102.0, 103.0, 104.0])
    engine = IndicatorEngine()
    request = [IndicatorRequest(name="SMA", params={"length": 3})]

    compute_indicators(engine, candles, request)
    assert engine.cache_size == 1

    compute_indicators(engine, candles, request)
    assert engine.cache_size == 1  # same (candles, params) -> cache hit, not a second entry
