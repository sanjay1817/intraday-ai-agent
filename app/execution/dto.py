"""Live Execution Engine input DTOs.

`ExecutionRequest` is the sole argument `app.execution.engine.ExecutionEngine`
accepts: an already-approved `RiskDecision` plus the `StrategySignal` it
was computed from. A `RiskDecision` alone has no symbol or side — it's a
verdict on something, not a self-contained order — and `StrategySignal`
doesn't track `exchange`, so `exchange` is supplied explicitly by the
caller, which already knows which exchange a symbol trades on.

Reuses `Exchange`/`OrderValidity`/`ProductType` from
`app.domain.enums.trading` — IOC/DAY are `OrderValidity` values, and
"Bracket"/"Cover Order" from the spec are `ProductType.BRACKET_ORDER`/
`ProductType.COVER_ORDER`, already modeled for the broker layer — and
`StrategySignal`/`RiskDecision` from the Strategy/Risk engines, rather
than re-deriving a trade's identity or its risk verdict.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums.trading import Exchange, OrderValidity, ProductType
from app.risk.models import RiskDecision
from app.strategy.models import StrategySignal


class ExecutionOrderType(StrEnum):
    """Pricing/trigger behavior for a live execution order.

    Distinct from `app.domain.enums.trading.OrderType` (what a broker's
    REST API accepts) the same way `app.paper.dto.PaperOrderType` is:
    `TRAILING_STOP`/`GTT` aren't necessarily one wire-level order a
    broker adapter sends as-is, so `order_builder.py` may decompose one
    `ExecutionRequest` into more than one real `OrderRequest` call.

    "Bracket"/"Cover Order" from the spec are deliberately *not* members
    here — they're `ProductType.BRACKET_ORDER`/`ProductType.COVER_ORDER`
    (`app.domain.enums.trading`), already modeled for the broker layer.
    "OCO" isn't a pricing behavior either; it's a relationship between
    two orders, tracked via `oco_group_id` on `app.execution.models.ExecutionOrder`.
    """

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    TRAILING_STOP = "TRAILING_STOP"
    GTT = "GTT"


class ExecutionRequest(BaseModel):
    """One risk-approved trade, ready to be built into a real broker order.

    `request_id`/`correlation_id` are the two idempotency identifiers
    supplied at request time; `execution_id` (assigned once this request
    enters the pipeline) and `retry_id` (assigned per retry attempt)
    live on `app.execution.models.ExecutionOrder` instead, since neither
    exists until execution actually begins.
    """

    model_config = ConfigDict(frozen=True)

    signal: StrategySignal
    risk_decision: RiskDecision
    exchange: Exchange
    product: ProductType = ProductType.INTRADAY
    preferred_order_type: ExecutionOrderType | None = None
    validity: OrderValidity = OrderValidity.DAY
    request_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_risk_decision_is_approved(self) -> "ExecutionRequest":
        """The Risk Engine is "the final authority before any trade
        reaches execution" per this project's core invariant — a
        rejected `RiskDecision` reaching this far would mean that
        authority was bypassed somewhere upstream, so this engine
        refuses to even construct a request around one.
        """

        if not self.risk_decision.approved:
            raise ValueError("ExecutionRequest requires an approved RiskDecision")
        return self
