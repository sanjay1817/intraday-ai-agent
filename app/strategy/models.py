"""Strategy Engine output models.

`StrategySignal` is what every `BaseStrategy.analyze()` returns — the
Strategy Engine's entire output surface, later merged (`ConfluenceResult`)
and ranked (`RankedSignal`) but never mutated in place.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums.trading import HistoricalInterval


class SignalDirection(StrEnum):
    """The trade direction a strategy is proposing for this bar."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NONE = "NONE"


class SignalStrength(StrEnum):
    """A coarse, human-readable setup-quality summary — distinct from
    `confidence` (0-100, granular) the same way a sentiment label is
    distinct from a raw sentiment score.
    """

    WEAK = "WEAK"
    MODERATE = "MODERATE"
    STRONG = "STRONG"


class StrategySignal(BaseModel):
    """One strategy's deterministic trade setup for one symbol/timeframe.

    Frozen: a signal is a point-in-time analysis result. Later stages
    (`signal_ranker.py`, `confluence.py`) wrap it in a new model
    (`RankedSignal`, `ConfluenceResult`) rather than mutating it.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    timeframe: HistoricalInterval
    direction: SignalDirection
    entry: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    targets: list[float] = Field(default_factory=list)
    risk_reward: float | None = Field(default=None, ge=0)
    confidence: float = Field(ge=0, le=100)
    strength: SignalStrength
    strategy_name: str = Field(min_length=1)
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    timestamp: datetime

    @model_validator(mode="after")
    def _check_directional_consistency(self) -> "StrategySignal":
        """A BULLISH signal's stop must sit below entry and every target
        above it, in ascending order (the mirror for BEARISH) — the same
        "is this an internally coherent trade plan" check
        `app.ai.decision_models.TradingDecision` applies to its own
        output, enforced here at the point setups are first produced.
        """

        if self.direction is SignalDirection.NONE:
            return self

        if self.entry is None or self.stop_loss is None:
            raise ValueError(f"{self.direction} signal requires both entry and stop_loss")

        if self.direction is SignalDirection.BULLISH:
            if not self.stop_loss < self.entry:
                raise ValueError("BULLISH signal requires stop_loss below entry")
            if any(target <= self.entry for target in self.targets):
                raise ValueError("BULLISH signal requires targets above entry")
            if self.targets != sorted(self.targets):
                raise ValueError("BULLISH signal targets must be in ascending order")
        else:  # SignalDirection.BEARISH
            if not self.stop_loss > self.entry:
                raise ValueError("BEARISH signal requires stop_loss above entry")
            if any(target >= self.entry for target in self.targets):
                raise ValueError("BEARISH signal requires targets below entry")
            if self.targets != sorted(self.targets, reverse=True):
                raise ValueError("BEARISH signal targets must be in descending order")

        return self


class ConfluenceResult(BaseModel):
    """The merged view of every strategy's signal for one symbol/timeframe:
    how many agree, in which direction, and the resulting combined
    confidence (see `app.strategy.confluence`).
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    timeframe: HistoricalInterval
    direction: SignalDirection
    agreeing_strategies: list[str] = Field(default_factory=list)
    conflicting_strategies: list[str] = Field(default_factory=list)
    confirmation_count: int = Field(ge=0)
    combined_confidence: float = Field(ge=0, le=100)
    signals: list[StrategySignal] = Field(default_factory=list)


class RankedSignal(BaseModel):
    """A `StrategySignal` plus its computed rank score and the factor
    breakdown that produced it (see `app.strategy.signal_ranker`).
    """

    model_config = ConfigDict(frozen=True)

    signal: StrategySignal
    rank_score: float = Field(ge=0)
    rank_factors: dict[str, float] = Field(default_factory=dict)
