"""Unit tests for `app.strategy.strategies.ema_trend`."""

from app.indicators.engine import IndicatorEngine
from app.indicators.schemas import ADXPoint, SingleValuePoint, SuperTrendPoint
from app.market.indicator_runtime import compute_indicators
from app.strategy.models import SignalDirection
from app.strategy.strategies.ema_trend import EMATrendStrategy

from .conftest import make_candles, make_context, single_result


def test_required_indicators_cover_ema_adx_supertrend() -> None:
    strategy = EMATrendStrategy()
    names = {request.name for request in strategy.required_indicators}

    assert names == {"EMA", "ADX", "SUPERTREND"}


def test_bullish_signal_on_a_real_confirmed_uptrend() -> None:
    # Empirically verified: a steady linear uptrend produces EMA_FAST >
    # EMA_SLOW, ADX >> 20, and SuperTrend direction=1 with its long_band
    # below the current price — every condition this strategy requires.
    closes = [100 + i * 0.5 for i in range(60)]
    candles = make_candles(closes)
    engine = IndicatorEngine()
    strategy = EMATrendStrategy()

    indicators = compute_indicators(engine, candles, strategy.required_indicators)
    context = make_context(candles, indicators)

    signal = strategy.analyze(context)

    assert signal.direction is SignalDirection.BULLISH
    assert signal.entry == closes[-1]
    assert signal.entry is not None
    assert signal.stop_loss is not None
    assert signal.stop_loss < signal.entry
    assert signal.targets == sorted(signal.targets)
    assert all(target > signal.entry for target in signal.targets)
    assert signal.confidence > 0
    assert signal.strategy_name == "EMA_TREND"


def test_bearish_signal_on_a_real_confirmed_downtrend() -> None:
    closes = [130 - i * 0.5 for i in range(60)]
    candles = make_candles(closes)
    engine = IndicatorEngine()
    strategy = EMATrendStrategy()

    indicators = compute_indicators(engine, candles, strategy.required_indicators)
    context = make_context(candles, indicators)

    signal = strategy.analyze(context)

    assert signal.direction is SignalDirection.BEARISH
    assert signal.entry is not None
    assert signal.stop_loss is not None
    assert signal.stop_loss > signal.entry
    assert signal.targets == sorted(signal.targets, reverse=True)
    assert all(target < signal.entry for target in signal.targets)


def test_no_signal_when_adx_below_threshold() -> None:
    candles = make_candles([100.0])
    indicators = {
        "EMA_FAST": single_result(SingleValuePoint(timestamp=candles[0].timestamp, value=101.0)),
        "EMA_SLOW": single_result(SingleValuePoint(timestamp=candles[0].timestamp, value=99.0)),
        "ADX": single_result(
            ADXPoint(
                timestamp=candles[0].timestamp, adx=15.0, adxr=15.0, plus_di=20.0, minus_di=10.0
            )
        ),
        "SUPERTREND": single_result(
            SuperTrendPoint(
                timestamp=candles[0].timestamp,
                value=99.0,
                direction=1,
                long_band=98.0,
                short_band=None,
            )
        ),
    }
    context = make_context(candles, indicators)

    signal = EMATrendStrategy().analyze(context)

    assert signal.direction is SignalDirection.NONE
    assert "ADX" in signal.reasons[0]


def test_no_signal_when_indicators_still_warming_up() -> None:
    candles = make_candles([100.0])
    indicators = {
        "EMA_FAST": single_result(SingleValuePoint(timestamp=candles[0].timestamp, value=None)),
        "EMA_SLOW": single_result(SingleValuePoint(timestamp=candles[0].timestamp, value=None)),
        "ADX": single_result(
            ADXPoint(
                timestamp=candles[0].timestamp, adx=None, adxr=None, plus_di=None, minus_di=None
            )
        ),
        "SUPERTREND": single_result(
            SuperTrendPoint(
                timestamp=candles[0].timestamp,
                value=None,
                direction=None,
                long_band=None,
                short_band=None,
            )
        ),
    }
    context = make_context(candles, indicators)

    signal = EMATrendStrategy().analyze(context)

    assert signal.direction is SignalDirection.NONE
    assert "warming up" in signal.reasons[0]


def test_no_signal_when_ema_and_supertrend_disagree() -> None:
    candles = make_candles([100.0])
    indicators = {
        # EMA cross says bullish (fast > slow)...
        "EMA_FAST": single_result(SingleValuePoint(timestamp=candles[0].timestamp, value=101.0)),
        "EMA_SLOW": single_result(SingleValuePoint(timestamp=candles[0].timestamp, value=99.0)),
        "ADX": single_result(
            ADXPoint(
                timestamp=candles[0].timestamp, adx=30.0, adxr=30.0, plus_di=25.0, minus_di=10.0
            )
        ),
        # ...but SuperTrend disagrees (direction=-1, a downtrend).
        "SUPERTREND": single_result(
            SuperTrendPoint(
                timestamp=candles[0].timestamp,
                value=101.0,
                direction=-1,
                long_band=None,
                short_band=102.0,
            )
        ),
    }
    context = make_context(candles, indicators)

    signal = EMATrendStrategy().analyze(context)

    assert signal.direction is SignalDirection.NONE
    assert "disagree" in signal.reasons[0]
