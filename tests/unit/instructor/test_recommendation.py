"""Unit tests for `app.instructor.recommendation`."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain.enums.trading import Exchange, HistoricalInterval
from app.instructor.recommendation import (
    InstructorRecommendation,
    RecommendationAction,
    generate_recommendation,
)
from app.strategy.models import ConfluenceResult, SignalDirection, SignalStrength, StrategySignal

_TIMESTAMP = datetime(2024, 1, 1, 9, 15, tzinfo=UTC)
_NOW = datetime(2024, 1, 1, 9, 20, tzinfo=UTC)


def _signal(
    strategy_name: str,
    direction: SignalDirection,
    *,
    confidence: float = 60.0,
    entry: float = 100.0,
    stop_loss: float | None = None,
    targets: list[float] | None = None,
    reasons: list[str] | None = None,
    warnings: list[str] | None = None,
) -> StrategySignal:
    if direction is SignalDirection.NONE:
        return StrategySignal(
            symbol="RELIANCE",
            timeframe=HistoricalInterval.FIVE_MINUTE,
            direction=direction,
            confidence=0.0,
            strength=SignalStrength.WEAK,
            strategy_name=strategy_name,
            reasons=reasons or [],
            warnings=warnings or [],
            timestamp=_TIMESTAMP,
        )

    resolved_stop = (
        stop_loss
        if stop_loss is not None
        else (95.0 if direction is SignalDirection.BULLISH else 105.0)
    )
    resolved_targets = (
        targets
        if targets is not None
        else ([110.0, 120.0] if direction is SignalDirection.BULLISH else [90.0, 80.0])
    )
    return StrategySignal(
        symbol="RELIANCE",
        timeframe=HistoricalInterval.FIVE_MINUTE,
        direction=direction,
        entry=entry,
        stop_loss=resolved_stop,
        targets=resolved_targets,
        confidence=confidence,
        strength=SignalStrength.MODERATE,
        strategy_name=strategy_name,
        reasons=reasons or [f"{strategy_name} fired"],
        warnings=warnings or [],
        timestamp=_TIMESTAMP,
    )


def _confluence(
    direction: SignalDirection,
    agreeing: list[StrategySignal],
    conflicting: list[StrategySignal],
    none_signals: list[StrategySignal] | None = None,
) -> ConfluenceResult:
    combined_confidence = (
        sum(signal.confidence for signal in agreeing) / len(agreeing) if agreeing else 0.0
    )
    return ConfluenceResult(
        symbol="RELIANCE",
        timeframe=HistoricalInterval.FIVE_MINUTE,
        direction=direction,
        agreeing_strategies=[signal.strategy_name for signal in agreeing],
        conflicting_strategies=[signal.strategy_name for signal in conflicting],
        confirmation_count=len(agreeing),
        combined_confidence=combined_confidence,
        signals=agreeing + conflicting + (none_signals or []),
    )


def test_bullish_confluence_produces_a_buy_recommendation() -> None:
    agreeing = [
        _signal(
            "EMA_TREND",
            SignalDirection.BULLISH,
            confidence=70.0,
            stop_loss=95.0,
            targets=[110.0, 120.0],
        ),
        _signal(
            "VWAP_VOLUME_BREAKOUT",
            SignalDirection.BULLISH,
            confidence=90.0,
            stop_loss=97.0,
            targets=[112.0, 118.0],
        ),
    ]
    conflicting = [_signal("RSI_MACD_REVERSAL", SignalDirection.BEARISH, confidence=40.0)]
    confluence = _confluence(SignalDirection.BULLISH, agreeing, conflicting)

    recommendation = generate_recommendation(confluence, Exchange.NSE, now=_NOW)

    assert recommendation.action is RecommendationAction.BUY
    assert recommendation.symbol == "RELIANCE"
    assert recommendation.exchange is Exchange.NSE
    assert recommendation.entry == pytest.approx(100.0)
    assert recommendation.stop_loss == pytest.approx(96.0)  # avg(95, 97)
    assert recommendation.targets == [pytest.approx(111.0), pytest.approx(119.0)]
    assert recommendation.confidence == pytest.approx(80.0)  # avg(70, 90)
    assert recommendation.confirmation_count == 2
    assert recommendation.total_strategy_count == 3
    assert "EMA_TREND" in recommendation.reasoning
    assert "VWAP_VOLUME_BREAKOUT" in recommendation.reasoning
    assert any("RSI_MACD_REVERSAL" in warning for warning in recommendation.warnings)
    assert recommendation.generated_at == _NOW


def test_bearish_confluence_produces_a_sell_recommendation() -> None:
    agreeing = [_signal("EMA_TREND", SignalDirection.BEARISH, confidence=65.0)]
    confluence = _confluence(SignalDirection.BEARISH, agreeing, [])

    recommendation = generate_recommendation(confluence, Exchange.NSE, now=_NOW)

    assert recommendation.action is RecommendationAction.SELL
    assert recommendation.entry is not None
    assert recommendation.stop_loss is not None
    assert recommendation.stop_loss > recommendation.entry
    assert all(target < recommendation.entry for target in recommendation.targets)


def test_no_setups_found_produces_a_hold_with_no_trade_plan() -> None:
    none_signals = [
        _signal("EMA_TREND", SignalDirection.NONE),
        _signal("RSI_MACD_REVERSAL", SignalDirection.NONE),
        _signal("VWAP_VOLUME_BREAKOUT", SignalDirection.NONE),
    ]
    confluence = _confluence(SignalDirection.NONE, [], [], none_signals)

    recommendation = generate_recommendation(confluence, Exchange.NSE, now=_NOW)

    assert recommendation.action is RecommendationAction.HOLD
    assert recommendation.entry is None
    assert recommendation.stop_loss is None
    assert recommendation.targets == []
    assert "no strategy found a qualifying setup" in recommendation.reasoning.lower()


def test_tied_disagreement_produces_a_hold_mentioning_the_conflict() -> None:
    bullish = _signal("EMA_TREND", SignalDirection.BULLISH)
    bearish = _signal("RSI_MACD_REVERSAL", SignalDirection.BEARISH)
    confluence = _confluence(SignalDirection.NONE, [], [bullish, bearish])

    recommendation = generate_recommendation(confluence, Exchange.NSE, now=_NOW)

    assert recommendation.action is RecommendationAction.HOLD
    assert "disagree" in recommendation.reasoning.lower()
    assert "EMA_TREND" in recommendation.reasoning
    assert "RSI_MACD_REVERSAL" in recommendation.reasoning


def test_minority_consensus_is_flagged_as_a_warning() -> None:
    agreeing = [_signal("EMA_TREND", SignalDirection.BULLISH, confidence=55.0)]
    none_signals = [
        _signal("RSI_MACD_REVERSAL", SignalDirection.NONE),
        _signal("VWAP_VOLUME_BREAKOUT", SignalDirection.NONE),
    ]
    confluence = _confluence(SignalDirection.BULLISH, agreeing, [], none_signals)

    recommendation = generate_recommendation(confluence, Exchange.NSE, now=_NOW)

    assert recommendation.action is RecommendationAction.BUY
    assert any("minority consensus" in warning.lower() for warning in recommendation.warnings)


def test_underlying_signal_warnings_are_propagated() -> None:
    agreeing = [
        _signal(
            "EMA_TREND",
            SignalDirection.BULLISH,
            warnings=["EMA cross is recent — trend may not be established yet."],
        )
    ]
    confluence = _confluence(SignalDirection.BULLISH, agreeing, [])

    recommendation = generate_recommendation(confluence, Exchange.NSE, now=_NOW)

    assert "EMA cross is recent — trend may not be established yet." in recommendation.warnings


def test_instructor_recommendation_rejects_inconsistent_buy_levels() -> None:
    with pytest.raises(ValidationError):
        InstructorRecommendation(
            symbol="RELIANCE",
            exchange=Exchange.NSE,
            timeframe=HistoricalInterval.FIVE_MINUTE,
            action=RecommendationAction.BUY,
            confidence=80.0,
            entry=100.0,
            stop_loss=105.0,  # invalid: above entry for a BUY
            targets=[110.0],
            confirmation_count=2,
            total_strategy_count=3,
            reasoning="test",
            generated_at=_NOW,
        )


def test_instructor_recommendation_allows_hold_with_no_trade_plan() -> None:
    recommendation = InstructorRecommendation(
        symbol="RELIANCE",
        exchange=Exchange.NSE,
        timeframe=HistoricalInterval.FIVE_MINUTE,
        action=RecommendationAction.HOLD,
        confidence=0.0,
        confirmation_count=0,
        total_strategy_count=3,
        reasoning="test",
        generated_at=_NOW,
    )

    assert recommendation.entry is None
    assert recommendation.stop_loss is None
