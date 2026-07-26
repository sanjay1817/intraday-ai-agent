"""AI Instructor: synthesizes the Strategy Engine's `ConfluenceResult`
into one deterministic, human-readable trade recommendation.

"Deterministic" is the operative word — this module never calls an LLM.
`app.ai.decision_models`'s own docstring anticipates a `prompt_builder.py`
that renders a prompt for a real language model; that is explicitly out
of scope for this milestone, which needs the recommendation's reasoning
to be exactly traceable back to which indicators/strategies fired, not
generated prose. "AI" in this milestone's name refers to the system's
overall role (algorithmic synthesis of technical signals into a trade
idea), not to an LLM call.

The model and the function that builds it live in the same file
deliberately — a separate `models.py` with nothing but this one Pydantic
class would be exactly the DTO-only file this milestone's rules prohibit.
"""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums.trading import Exchange, HistoricalInterval
from app.strategy.models import ConfluenceResult, SignalDirection, StrategySignal


class RecommendationAction(StrEnum):
    """What the Intraday AI Instructor is telling a human trader to do.

    Deliberately narrower than `app.ai.decision_models.TradingAction`
    (`BUY`/`SELL`/`EXIT`/`HOLD`/`NO_TRADE`): this milestone never manages
    an open position (no `EXIT`), and doesn't distinguish "found no
    setup" from "found conflicting setups" at the `action` level — both
    are `HOLD`; that distinction lives in `reasoning`/`warnings` instead.
    """

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


_DIRECTION_TO_ACTION = {
    SignalDirection.BULLISH: RecommendationAction.BUY,
    SignalDirection.BEARISH: RecommendationAction.SELL,
    SignalDirection.NONE: RecommendationAction.HOLD,
}


class InstructorRecommendation(BaseModel):
    """One symbol's actionable intraday recommendation — the Intraday AI
    Instructor's entire output surface, and what the Signal API returns.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    exchange: Exchange
    timeframe: HistoricalInterval
    action: RecommendationAction
    confidence: float = Field(ge=0, le=100)
    entry: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    targets: list[float] = Field(default_factory=list)
    risk_reward: float | None = Field(default=None, ge=0)
    confirmation_count: int = Field(ge=0)
    total_strategy_count: int = Field(gt=0)
    reasoning: str = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)
    generated_at: datetime

    @model_validator(mode="after")
    def _check_price_levels_are_directionally_consistent(self) -> "InstructorRecommendation":
        """The same "is this an internally coherent trade plan" check
        `app.strategy.models.StrategySignal`/`app.ai.decision_models
        .TradingDecision` already apply to their own output — enforced
        again here since this is the final, human-facing artifact.
        """

        if self.action is RecommendationAction.HOLD:
            return self

        if self.entry is None or self.stop_loss is None:
            raise ValueError(f"{self.action} requires both entry and stop_loss")

        if self.action is RecommendationAction.BUY:
            if not self.stop_loss < self.entry:
                raise ValueError("BUY requires stop_loss below entry")
            if any(target <= self.entry for target in self.targets):
                raise ValueError("BUY requires targets above entry")
            if self.targets != sorted(self.targets):
                raise ValueError("BUY requires targets in ascending order")
        else:  # RecommendationAction.SELL
            if not self.stop_loss > self.entry:
                raise ValueError("SELL requires stop_loss above entry")
            if any(target >= self.entry for target in self.targets):
                raise ValueError("SELL requires targets below entry")
            if self.targets != sorted(self.targets, reverse=True):
                raise ValueError("SELL requires targets in descending order")

        return self


def generate_recommendation(
    confluence: ConfluenceResult, exchange: Exchange, *, now: datetime | None = None
) -> InstructorRecommendation:
    """Synthesize `confluence` into one actionable recommendation.

    Args:
        confluence: The Strategy Engine's merged view across every
            registered strategy for one symbol/timeframe.
        exchange: Which exchange `confluence.symbol` trades on (not
            itself part of `ConfluenceResult`, which is exchange-agnostic).
        now: When this recommendation was generated; defaults to the
            real current time.
    """

    action = _DIRECTION_TO_ACTION[confluence.direction]
    agreeing_signals = [
        signal
        for signal in confluence.signals
        if signal.strategy_name in confluence.agreeing_strategies
    ]

    entry, stop_loss, targets, risk_reward = _consolidate_trade_plan(action, agreeing_signals)
    warnings = _build_warnings(confluence)

    if action is not RecommendationAction.HOLD and entry is None:
        # Every current strategy always populates entry/stop/targets for
        # a non-NONE signal, so this is unreachable today — kept as a
        # safety net against a future strategy whose `StrategySignal`
        # satisfies its own validator (targets aren't required to be
        # non-empty) but leaves nothing to consolidate here.
        action = RecommendationAction.HOLD
        warnings.append(
            "Downgraded to HOLD: agreeing strategies did not provide a complete trade plan."
        )

    reasoning = _build_reasoning(confluence, action, agreeing_signals)

    return InstructorRecommendation(
        symbol=confluence.symbol,
        exchange=exchange,
        timeframe=confluence.timeframe,
        action=action,
        confidence=confluence.combined_confidence,
        entry=entry,
        stop_loss=stop_loss,
        targets=targets,
        risk_reward=risk_reward,
        confirmation_count=confluence.confirmation_count,
        total_strategy_count=len(confluence.signals),
        reasoning=reasoning,
        warnings=warnings,
        generated_at=now or datetime.now(UTC),
    )


def _consolidate_trade_plan(
    action: RecommendationAction, agreeing_signals: list[StrategySignal]
) -> tuple[float | None, float | None, list[float], float | None]:
    """Average the agreeing strategies' entry/stop/targets into one
    trade plan.

    Every current strategy uses the same latest candle's close as
    `entry`, so averaging it is a no-op in practice; stop-loss and
    targets genuinely differ per strategy (a SuperTrend band, a swing
    high/low, VWAP), and averaging them is a simple, transparent way to
    reconcile several independently-reasonable levels into one, rather
    than arbitrarily favoring one strategy's numbers over another's.
    """

    if action is RecommendationAction.HOLD or not agreeing_signals:
        return None, None, [], None

    entries = [signal.entry for signal in agreeing_signals if signal.entry is not None]
    stops = [signal.stop_loss for signal in agreeing_signals if signal.stop_loss is not None]
    target_lists = [signal.targets for signal in agreeing_signals if signal.targets]

    if not entries or not stops or not target_lists:
        return None, None, [], None

    entry = sum(entries) / len(entries)
    stop_loss = sum(stops) / len(stops)

    target_count = min(len(targets) for targets in target_lists)
    targets = [
        sum(targets[i] for targets in target_lists) / len(target_lists) for i in range(target_count)
    ]

    risk = abs(entry - stop_loss)
    risk_reward = abs(targets[0] - entry) / risk if targets and risk > 0 else None

    return entry, stop_loss, targets, risk_reward


def _build_reasoning(
    confluence: ConfluenceResult,
    action: RecommendationAction,
    agreeing_signals: list[StrategySignal],
) -> str:
    if action is RecommendationAction.HOLD:
        if confluence.conflicting_strategies:
            return (
                f"HOLD: strategies disagree on direction "
                f"({', '.join(confluence.conflicting_strategies)})."
            )
        return "HOLD: no strategy found a qualifying setup."

    header = (
        f"{action.value}: {confluence.confirmation_count} of {len(confluence.signals)} "
        f"strategies agree ({confluence.direction.value})."
    )
    strategy_reasons = [
        f"{signal.strategy_name}: {'; '.join(signal.reasons)}"
        for signal in agreeing_signals
        if signal.reasons
    ]
    return " ".join([header, *strategy_reasons])


def _build_warnings(confluence: ConfluenceResult) -> list[str]:
    warnings: list[str] = []

    if confluence.conflicting_strategies:
        warnings.append(
            f"{len(confluence.conflicting_strategies)} strategy(ies) disagreed: "
            f"{', '.join(confluence.conflicting_strategies)}."
        )

    total = len(confluence.signals)
    if (
        confluence.direction is not SignalDirection.NONE
        and confluence.confirmation_count * 2 <= total
    ):
        warnings.append(
            f"Only {confluence.confirmation_count} of {total} strategies confirmed this "
            "signal — a minority consensus, treat with extra caution."
        )

    for signal in confluence.signals:
        warnings.extend(signal.warnings)

    return warnings
