"""Unit tests for `app.strategy.engine`."""

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from app.domain.enums.trading import Exchange, HistoricalInterval
from app.domain.exceptions.indicators import InvalidOHLCVDataError
from app.indicators.engine import IndicatorRequest
from app.market.dto import MarketSessionState
from app.strategy.base_strategy import BaseStrategy
from app.strategy.dto import StrategyContext
from app.strategy.engine import StrategyEngine, _merge_into_confluence
from app.strategy.models import SignalDirection, SignalStrength, StrategySignal

from .strategies.conftest import make_candles


class _FixedStrategy(BaseStrategy):
    """A strategy that always returns a pre-determined direction — for
    exercising `StrategyEngine`'s merge logic without real indicators.
    """

    def __init__(self, strategy_name: str, direction: SignalDirection, confidence: float) -> None:
        self._strategy_name = strategy_name
        self._direction = direction
        self._confidence = confidence

    @property
    def name(self) -> str:
        return self._strategy_name

    @property
    def required_indicators(self) -> Sequence[IndicatorRequest]:
        # This stub ignores indicators entirely (always returns a fixed
        # direction), so it declares none — a real strategy that read
        # `context.primary_snapshot.indicators` would need to request a
        # low-warm-up indicator here to work against the tiny candle
        # counts these tests use.
        return []

    def analyze(self, context: StrategyContext) -> StrategySignal:
        if self._direction is SignalDirection.NONE:
            return self._no_signal(context, "stub: no setup")
        return StrategySignal(
            symbol=context.symbol,
            timeframe=context.primary_timeframe,
            direction=self._direction,
            entry=100.0,
            stop_loss=95.0 if self._direction is SignalDirection.BULLISH else 105.0,
            targets=(
                [110.0, 120.0] if self._direction is SignalDirection.BULLISH else [90.0, 80.0]
            ),
            confidence=self._confidence,
            strength=SignalStrength.MODERATE,
            strategy_name=self._strategy_name,
            timestamp=context.latest_candle.timestamp,
        )


def _signal(direction: SignalDirection, name: str, confidence: float) -> StrategySignal:
    if direction is SignalDirection.NONE:
        entry, stop_loss, targets = None, None, []
    elif direction is SignalDirection.BULLISH:
        entry, stop_loss, targets = 100.0, 95.0, [110.0, 120.0]
    else:
        entry, stop_loss, targets = 100.0, 105.0, [90.0, 80.0]

    return StrategySignal(
        symbol="TEST",
        timeframe=HistoricalInterval.FIVE_MINUTE,
        direction=direction,
        entry=entry,
        stop_loss=stop_loss,
        targets=targets,
        confidence=confidence,
        strength=SignalStrength.MODERATE,
        strategy_name=name,
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )


def test_required_indicator_requests_deduplicates_across_default_strategies() -> None:
    engine = StrategyEngine()

    result_keys = {request.result_key for request in engine.required_indicator_requests}

    assert result_keys == {
        "EMA_FAST",
        "EMA_SLOW",
        "ADX",
        "SUPERTREND",
        "RSI",
        "MACD",
        "VWAP",
        "VOLUME_SMA",
    }


def test_merge_prefers_majority_direction() -> None:
    signals = [
        _signal(SignalDirection.BULLISH, "A", 60.0),
        _signal(SignalDirection.BULLISH, "B", 80.0),
        _signal(SignalDirection.BEARISH, "C", 50.0),
    ]

    result = _merge_into_confluence("TEST", HistoricalInterval.FIVE_MINUTE, signals)

    assert result.direction is SignalDirection.BULLISH
    assert result.agreeing_strategies == ["A", "B"]
    assert result.conflicting_strategies == ["C"]
    assert result.confirmation_count == 2
    assert result.combined_confidence == pytest.approx(70.0)


def test_merge_resolves_a_tie_to_none() -> None:
    signals = [
        _signal(SignalDirection.BULLISH, "A", 60.0),
        _signal(SignalDirection.BEARISH, "B", 60.0),
    ]

    result = _merge_into_confluence("TEST", HistoricalInterval.FIVE_MINUTE, signals)

    assert result.direction is SignalDirection.NONE
    assert result.agreeing_strategies == []
    assert set(result.conflicting_strategies) == {"A", "B"}
    assert result.confirmation_count == 0
    assert result.combined_confidence == 0.0


def test_merge_of_all_none_signals_is_none_with_zero_confidence() -> None:
    signals = [
        _signal(SignalDirection.NONE, "A", 0.0),
        _signal(SignalDirection.NONE, "B", 0.0),
        _signal(SignalDirection.NONE, "C", 0.0),
    ]

    result = _merge_into_confluence("TEST", HistoricalInterval.FIVE_MINUTE, signals)

    assert result.direction is SignalDirection.NONE
    assert result.confirmation_count == 0
    assert result.combined_confidence == 0.0
    assert len(result.signals) == 3


def test_analyze_symbol_with_injected_stub_strategies() -> None:
    engine = StrategyEngine(
        strategies=[
            _FixedStrategy("BULL_A", SignalDirection.BULLISH, 70.0),
            _FixedStrategy("BULL_B", SignalDirection.BULLISH, 90.0),
            _FixedStrategy("BEAR_C", SignalDirection.BEARISH, 40.0),
        ]
    )
    candles = make_candles([100.0, 101.0, 102.0])

    result = engine.analyze_symbol(
        "TEST",
        Exchange.NSE,
        HistoricalInterval.FIVE_MINUTE,
        candles,
        MarketSessionState.OPEN,
    )

    assert result.direction is SignalDirection.BULLISH
    assert result.confirmation_count == 2
    assert result.combined_confidence == pytest.approx(80.0)
    assert len(result.signals) == 3


def test_analyze_symbol_raises_on_empty_candles() -> None:
    engine = StrategyEngine(strategies=[_FixedStrategy("A", SignalDirection.NONE, 0.0)])

    with pytest.raises(InvalidOHLCVDataError):
        engine.analyze_symbol(
            "TEST", Exchange.NSE, HistoricalInterval.FIVE_MINUTE, [], MarketSessionState.OPEN
        )


def test_analyze_symbol_with_real_default_strategies_on_a_confirmed_uptrend() -> None:
    # Reuses the same empirically-verified uptrend from the EMA_TREND
    # strategy's own tests; only asserts the engine wires everything
    # together correctly, not each strategy's internal logic again.
    closes = [100 + i * 0.5 for i in range(60)]
    candles = make_candles(closes)
    engine = StrategyEngine()

    result = engine.analyze_symbol(
        "TEST", Exchange.NSE, HistoricalInterval.FIVE_MINUTE, candles, MarketSessionState.OPEN
    )

    assert len(result.signals) == 3
    assert {signal.strategy_name for signal in result.signals} == {
        "EMA_TREND",
        "RSI_MACD_REVERSAL",
        "VWAP_VOLUME_BREAKOUT",
    }
