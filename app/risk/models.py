"""Risk Management Engine output model.

`RiskDecision` is `app.risk.engine.RiskEngine`'s entire output surface —
the final authority's verdict on one candidate trade. Neither the
Strategy Engine nor the AI Decision Engine may bypass it, and the Live
Execution Engine (`app.execution`) executes only an approved
`RiskDecision`.
"""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RiskDecision(BaseModel):
    """The Risk Management Engine's verdict on one candidate trade.

    Frozen: a decision is a point-in-time record, the same way
    `StrategySignal` and `TradingDecision` are in their own engines.
    """

    model_config = ConfigDict(frozen=True)

    approved: bool
    position_size: int = Field(ge=0)
    adjusted_stop_loss: float | None = Field(default=None, gt=0)
    adjusted_targets: list[float] = Field(default_factory=list)
    risk_score: float = Field(ge=0, le=100)
    rejection_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_approval_is_internally_consistent(self) -> "RiskDecision":
        """An approved decision must actually size a position, and a
        rejected one must say why. `approved=True` with
        `position_size=0`, or `approved=False` with no
        `rejection_reason`, would be a bug in whatever produced this
        decision, not a legitimate risk verdict.
        """

        if self.approved and self.position_size <= 0:
            raise ValueError("an approved RiskDecision must have position_size > 0")
        if not self.approved and self.rejection_reason is None:
            raise ValueError("a rejected RiskDecision must include rejection_reason")
        return self
