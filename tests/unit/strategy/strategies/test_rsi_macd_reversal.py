"""Unit tests for `app.strategy.strategies.rsi_macd_reversal`."""

from typing import Any

from app.indicators.engine import IndicatorEngine
from app.indicators.schemas import IndicatorResult, MACDPoint, SingleValuePoint
from app.market.indicator_runtime import compute_indicators
from app.strategy.models import SignalDirection
from app.strategy.strategies.rsi_macd_reversal import RSIMACDReversalStrategy

from .conftest import make_candles, make_context, single_result


def test_required_indicators_cover_rsi_and_macd() -> None:
    strategy = RSIMACDReversalStrategy()
    names = {request.name for request in strategy.required_indicators}

    assert names == {"RSI", "MACD"}


def test_bullish_signal_on_a_real_oversold_recovery() -> None:
    # Empirically verified: a 30-bar decline followed by exactly 4 bars
    # of recovery lands the RSI(14) crossover (27.19 -> 34.11, through
    # the 30 threshold) precisely on the last bar, with a positive MACD
    # histogram confirming it.
    decline = [130 - i * 1.0 for i in range(30)]
    recover = [decline[-1] + i * 1.5 for i in range(1, 5)]
    candles = make_candles(decline + recover)
    engine = IndicatorEngine()
    strategy = RSIMACDReversalStrategy()

    indicators = compute_indicators(engine, candles, strategy.required_indicators)
    context = make_context(candles, indicators)

    signal = strategy.analyze(context)

    assert signal.direction is SignalDirection.BULLISH
    assert signal.entry is not None
    assert signal.stop_loss is not None
    assert signal.stop_loss < signal.entry
    assert all(target > signal.entry for target in signal.targets)
    assert signal.strategy_name == "RSI_MACD_REVERSAL"


def test_bearish_signal_on_a_real_overbought_reversal() -> None:
    rally = [70 + i * 1.0 for i in range(30)]
    decline = [rally[-1] - i * 1.5 for i in range(1, 5)]
    candles = make_candles(rally + decline)
    engine = IndicatorEngine()
    strategy = RSIMACDReversalStrategy()

    indicators = compute_indicators(engine, candles, strategy.required_indicators)
    context = make_context(candles, indicators)

    signal = strategy.analyze(context)

    assert signal.direction is SignalDirection.BEARISH
    assert signal.entry is not None
    assert signal.stop_loss is not None
    assert signal.stop_loss > signal.entry
    assert all(target < signal.entry for target in signal.targets)


def test_no_signal_when_rsi_has_not_crossed_the_threshold() -> None:
    candles = make_candles([100.0, 101.0])
    indicators: dict[str, IndicatorResult[Any]] = {
        "RSI": IndicatorResult(
            name="RSI",
            params={},
            values=[
                SingleValuePoint(timestamp=candles[0].timestamp, value=45.0),
                SingleValuePoint(timestamp=candles[1].timestamp, value=48.0),
            ],
        ),
        "MACD": IndicatorResult(
            name="MACD",
            params={},
            values=[
                MACDPoint(timestamp=candles[0].timestamp, macd=0.1, histogram=0.05, signal=0.05),
                MACDPoint(timestamp=candles[1].timestamp, macd=0.2, histogram=0.1, signal=0.1),
            ],
        ),
    }
    context = make_context(candles, indicators)

    signal = RSIMACDReversalStrategy().analyze(context)

    assert signal.direction is SignalDirection.NONE
    assert "No RSI extreme-crossover" in signal.reasons[0]


def test_no_signal_when_indicators_still_warming_up() -> None:
    candles = make_candles([100.0, 101.0])
    indicators: dict[str, IndicatorResult[Any]] = {
        "RSI": IndicatorResult(
            name="RSI",
            params={},
            values=[
                SingleValuePoint(timestamp=candles[0].timestamp, value=None),
                SingleValuePoint(timestamp=candles[1].timestamp, value=None),
            ],
        ),
        "MACD": IndicatorResult(
            name="MACD",
            params={},
            values=[
                MACDPoint(timestamp=candles[0].timestamp, macd=None, histogram=None, signal=None),
                MACDPoint(timestamp=candles[1].timestamp, macd=None, histogram=None, signal=None),
            ],
        ),
    }
    context = make_context(candles, indicators)

    signal = RSIMACDReversalStrategy().analyze(context)

    assert signal.direction is SignalDirection.NONE
    assert "warming up" in signal.reasons[0]


def test_no_signal_when_too_few_bars_for_a_crossover() -> None:
    candles = make_candles([100.0])
    indicators = {
        "RSI": single_result(SingleValuePoint(timestamp=candles[0].timestamp, value=25.0)),
        "MACD": single_result(
            MACDPoint(timestamp=candles[0].timestamp, macd=0.1, histogram=0.1, signal=0.05)
        ),
    }
    context = make_context(candles, indicators)

    signal = RSIMACDReversalStrategy().analyze(context)

    assert signal.direction is SignalDirection.NONE
    assert "Not enough history" in signal.reasons[0]
