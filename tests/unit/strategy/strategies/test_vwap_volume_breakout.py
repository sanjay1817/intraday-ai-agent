"""Unit tests for `app.strategy.strategies.vwap_volume_breakout`."""

from typing import Any

from app.indicators.engine import IndicatorEngine
from app.indicators.schemas import IndicatorResult, SingleValuePoint
from app.market.indicator_runtime import compute_indicators
from app.strategy.models import SignalDirection
from app.strategy.strategies.vwap_volume_breakout import VWAPVolumeBreakoutStrategy

from .conftest import make_candles, make_context


def test_required_indicators_cover_vwap_and_volume_sma() -> None:
    strategy = VWAPVolumeBreakoutStrategy()
    names = {request.name for request in strategy.required_indicators}

    assert names == {"VWAP", "VOLUME_SMA"}


def test_bullish_signal_on_a_real_vwap_breakout_with_volume() -> None:
    # Empirically verified: 25 flat-to-slightly-declining bars keep price
    # at/below VWAP, then a final bar breaks above VWAP on 2.7x average
    # volume — both conditions this strategy requires.
    closes = [100.0 - 0.05 * i for i in range(25)] + [101.5]
    volumes = [1000] * 25 + [3000]
    candles = make_candles(closes, volumes=volumes)
    engine = IndicatorEngine()
    strategy = VWAPVolumeBreakoutStrategy()

    indicators = compute_indicators(engine, candles, strategy.required_indicators)
    context = make_context(candles, indicators)

    signal = strategy.analyze(context)

    assert signal.direction is SignalDirection.BULLISH
    assert signal.entry == closes[-1]
    assert signal.entry is not None
    assert signal.stop_loss is not None
    assert signal.stop_loss < signal.entry
    assert all(target > signal.entry for target in signal.targets)
    assert signal.strategy_name == "VWAP_VOLUME_BREAKOUT"


def test_bearish_signal_on_a_real_vwap_breakdown_with_volume() -> None:
    closes = [100.0 + 0.05 * i for i in range(25)] + [98.5]
    volumes = [1000] * 25 + [3000]
    candles = make_candles(closes, volumes=volumes)
    engine = IndicatorEngine()
    strategy = VWAPVolumeBreakoutStrategy()

    indicators = compute_indicators(engine, candles, strategy.required_indicators)
    context = make_context(candles, indicators)

    signal = strategy.analyze(context)

    assert signal.direction is SignalDirection.BEARISH
    assert signal.entry is not None
    assert signal.stop_loss is not None
    assert signal.stop_loss > signal.entry
    assert all(target < signal.entry for target in signal.targets)


def test_no_signal_when_volume_does_not_confirm_the_cross() -> None:
    # Same price cross as the bullish test, but ordinary (not spiking) volume.
    closes = [100.0 - 0.05 * i for i in range(25)] + [101.5]
    volumes = [1000] * 26
    candles = make_candles(closes, volumes=volumes)
    engine = IndicatorEngine()
    strategy = VWAPVolumeBreakoutStrategy()

    indicators = compute_indicators(engine, candles, strategy.required_indicators)
    context = make_context(candles, indicators)

    signal = strategy.analyze(context)

    assert signal.direction is SignalDirection.NONE
    assert "No VWAP cross" in signal.reasons[0]


def test_no_signal_when_indicators_still_warming_up() -> None:
    candles = make_candles([100.0, 101.0], volumes=[1000, 1000])
    indicators: dict[str, IndicatorResult[Any]] = {
        "VWAP": IndicatorResult(
            name="VWAP",
            params={},
            values=[
                SingleValuePoint(timestamp=candles[0].timestamp, value=None),
                SingleValuePoint(timestamp=candles[1].timestamp, value=None),
            ],
        ),
        "VOLUME_SMA": IndicatorResult(
            name="VOLUME_SMA",
            params={},
            values=[
                SingleValuePoint(timestamp=candles[0].timestamp, value=None),
                SingleValuePoint(timestamp=candles[1].timestamp, value=None),
            ],
        ),
    }
    context = make_context(candles, indicators)

    signal = VWAPVolumeBreakoutStrategy().analyze(context)

    assert signal.direction is SignalDirection.NONE
    assert "warming up" in signal.reasons[0]


def test_no_signal_with_only_one_candle() -> None:
    candles = make_candles([100.0], volumes=[1000])
    indicators: dict[str, IndicatorResult[Any]] = {
        "VWAP": IndicatorResult(
            name="VWAP",
            params={},
            values=[SingleValuePoint(timestamp=candles[0].timestamp, value=100.0)],
        ),
        "VOLUME_SMA": IndicatorResult(
            name="VOLUME_SMA",
            params={},
            values=[SingleValuePoint(timestamp=candles[0].timestamp, value=1000.0)],
        ),
    }
    context = make_context(candles, indicators)

    signal = VWAPVolumeBreakoutStrategy().analyze(context)

    assert signal.direction is SignalDirection.NONE
    assert "Not enough history" in signal.reasons[0]
