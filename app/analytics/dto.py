"""Portfolio Analytics & Performance Intelligence input DTOs.

`PortfolioTradeRecord` is this module's own self-contained "one closed
trade" shape. Paper Trading Engine's `ClosedTrade` (`app.paper`) and the
Backtesting Framework's trade-result model (`app.backtest`) don't exist
yet — both stopped at earlier files — and this module's job is
explicitly retrospective ("evaluates trading performance... never
executes trades"), so it defines the trade-history shape it needs rather
than waiting on either. Once those models exist, a thin mapping function
(not a redefinition of this module) will convert to `PortfolioTradeRecord`;
this is the same reasoning `app.backtest.metrics`'s own trade shape was
built on.

`AIDecisionRecord` is similarly self-contained relative to
`app.ai_agents`: it needs the outcome-linked subset of a decision (was
the predicted direction actually right), which is a distinct,
analytics-specific shape from `AgentOpinion`/`MergedDecision`.

Reuses `Exchange`/`OrderSide` (`app.domain.enums.trading`) and
`MarketCandle` (`app.market.dto`) for benchmark price series.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums.trading import Exchange, OrderSide
from app.market.dto import MarketCandle


class ReportFormat(StrEnum):
    """Output formats `report_generator.py` supports."""

    HTML = "HTML"
    PDF = "PDF"
    EXCEL = "EXCEL"
    CSV = "CSV"
    JSON = "JSON"


class PortfolioTradeRecord(BaseModel):
    """One closed, round-trip trade, tagged with everything
    `attribution.py`/`strategy_comparison.py` group by.

    The tag fields (`strategy_name`, `market_regime`, `timeframe`,
    `session`, `sector`) are all optional: a trade history assembled
    from a simpler source (e.g. a single-strategy paper-trading run)
    won't have every dimension populated, and attribution/comparison
    modules skip a record for whichever dimension it lacks rather than
    requiring the caller to backfill data that was never captured.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    exchange: Exchange
    side: OrderSide
    quantity: int = Field(gt=0)
    entry_price: float = Field(gt=0)
    exit_price: float = Field(gt=0)
    entry_timestamp: datetime
    exit_timestamp: datetime
    gross_pnl: float
    commission: float = Field(ge=0)
    net_pnl: float
    strategy_name: str | None = None
    market_regime: str | None = None
    timeframe: str | None = None
    session: str | None = None
    sector: str | None = None

    @model_validator(mode="after")
    def _check_exit_after_entry(self) -> "PortfolioTradeRecord":
        if self.exit_timestamp <= self.entry_timestamp:
            raise ValueError("exit_timestamp must be after entry_timestamp")
        return self

    @property
    def holding_duration_minutes(self) -> float:
        """Wall-clock time the position was held, in minutes."""

        return (self.exit_timestamp - self.entry_timestamp).total_seconds() / 60.0


class AIDecisionRecord(BaseModel):
    """One AI-assisted decision's outcome, for `ai_accuracy.py`."""

    model_config = ConfigDict(frozen=True)

    decision_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    predicted_direction: OrderSide | None = None
    actual_outcome_direction: OrderSide | None = None
    confidence: float = Field(ge=0, le=100)
    prompt_version: str | None = None
    agent_agreement_count: int = Field(ge=0)
    agent_disagreement_count: int = Field(ge=0)
    latency_ms: float = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    timestamp: datetime

    @property
    def was_correct(self) -> bool | None:
        """Whether the predicted direction matched the actual outcome,
        or `None` if either side of that comparison is unknown (e.g. a
        HOLD/NO_TRADE decision has no directional prediction to grade).
        """

        if self.predicted_direction is None or self.actual_outcome_direction is None:
            return None
        return self.predicted_direction == self.actual_outcome_direction


class OptimizationTrialResult(BaseModel):
    """One parameter-optimization trial's result, for
    `optimization_report.py`.

    Self-contained the same way `PortfolioTradeRecord` is:
    `app.backtest.optimization` doesn't exist yet (it needs a working
    backtest loop), but the shape of "one trial's parameters and the
    metric they produced" doesn't depend on that loop's implementation.
    """

    model_config = ConfigDict(frozen=True)

    parameters: dict[str, float] = Field(min_length=1)
    objective_metric_name: str = Field(min_length=1)
    objective_value: float
    total_trades: int = Field(ge=0)


class BenchmarkSeries(BaseModel):
    """A benchmark instrument's price history, for `benchmark.py`'s
    buy-and-hold / index comparisons.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    exchange: Exchange
    candles: list[MarketCandle] = Field(min_length=2)
