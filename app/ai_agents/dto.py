"""Multi-Agent AI Intelligence System input DTOs.

Each specialized agent (`app.ai_agents.agents.*`) receives its own
narrow context — never the raw, everything-at-once context a single
general-purpose LLM call would need. That's the point of this system:
many small, focused prompts instead of one large one. Every context
inherits `AgentContext` (symbol/exchange/as-of-time) and adds only the
fields its agent actually reasons about.

Reuses `MarketCandle`/`Exchange`/`OrderSide` (`app.market.dto`,
`app.domain.enums.trading`), `IndicatorResult` (`app.indicators.schemas`),
and `RiskDecision`/`PositionExposure` (`app.risk.models`/`app.risk.dto`)
rather than re-modeling data those engines already produce.

`ExecutionReviewContext`/`TradeReviewContext` are real, fully-specified
DTOs even though no live execution-monitoring or trade-history system
exists yet to populate them automatically — consistent with every other
engine in this project, these agents don't fetch their own data; a
caller assembles the context and hands it over.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums.trading import Exchange, OrderSide
from app.indicators.schemas import IndicatorResult
from app.market.dto import MarketCandle
from app.risk.dto import PositionExposure
from app.risk.models import RiskDecision


class AgentContext(BaseModel):
    """Fields every specialized agent's context shares."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    exchange: Exchange
    as_of: datetime


class MarketRegimeContext(AgentContext):
    """Input for `agents.market_regime_agent`: classify the current
    market regime (trending, range, volatile, low-liquidity, breakout,
    reversal, or a point in a broader market cycle) from recent
    price/volume behavior.
    """

    candles: list[MarketCandle] = Field(min_length=1)
    indicators: dict[str, IndicatorResult[Any]] = Field(default_factory=dict)


class TechnicalContext(AgentContext):
    """Input for `agents.technical_agent`: evaluate the standard
    indicator set plus price action against known support/resistance
    levels, and return a technical confidence score.
    """

    candles: list[MarketCandle] = Field(min_length=1)
    indicators: dict[str, IndicatorResult[Any]] = Field(default_factory=dict)
    support_levels: list[float] = Field(default_factory=list)
    resistance_levels: list[float] = Field(default_factory=list)


class SentimentContext(AgentContext):
    """Input for `agents.sentiment_agent`: market psychology — social
    sentiment and the fear/greed backdrop — independent of specific news
    items (see `NewsContext`) or capital-flow/macro data (see
    `MacroContext`).
    """

    social_sentiment_score: float | None = Field(default=None, ge=-1.0, le=1.0)
    fear_greed_index: float | None = Field(default=None, ge=0, le=100)


class NewsContext(AgentContext):
    """Input for `agents.news_agent`: recent news specific to this
    symbol/sector, analyzed independently of broader social mood.
    """

    headlines: list[str] = Field(default_factory=list)
    article_summaries: list[str] = Field(default_factory=list)


class MacroContext(AgentContext):
    """Input for `agents.macro_agent`: capital-flow and sector-level
    backdrop — FII/DII net flow and relative sector strength.
    """

    fii_net_flow: float | None = None
    dii_net_flow: float | None = None
    sector_relative_strength: float | None = Field(default=None, ge=-1.0, le=1.0)


class VolatilityContext(AgentContext):
    """Input for `agents.volatility_agent`: the current volatility
    regime specifically — expansion vs. contraction — distinct from
    `MarketRegimeContext`'s broader trend/range classification.
    """

    indicators: dict[str, IndicatorResult[Any]] = Field(default_factory=dict)
    implied_volatility: float | None = Field(default=None, ge=0)
    historical_volatility_percentile: float | None = Field(default=None, ge=0, le=100)


class RiskReviewContext(AgentContext):
    """Input for `agents.risk_review_agent`: the deterministic Risk
    Engine's verdict plus current portfolio exposure, for a second,
    AI-driven opinion layered on top of it — never a replacement for it.
    """

    risk_decision: RiskDecision
    current_positions: list[PositionExposure] = Field(default_factory=list)
    correlation_with_existing_positions: float | None = Field(default=None, ge=-1.0, le=1.0)
    capital_utilization_percent: float = Field(ge=0, le=100)


class ExecutionReviewContext(AgentContext):
    """Input for `agents.execution_review_agent`: what actually happened
    when an order was placed, for post-trade execution-quality review.
    """

    order_price: float = Field(gt=0)
    fill_price: float = Field(gt=0)
    spread_percent: float | None = Field(default=None, ge=0)
    liquidity_score: float | None = Field(default=None, ge=0, le=100)
    broker_healthy: bool = True
    execution_latency_ms: float = Field(ge=0)


class TradeReviewContext(AgentContext):
    """Input for `agents.trade_review_agent`: one completed round-trip
    trade, for post-mortem analysis and lesson extraction.
    """

    side: OrderSide
    entry_price: float = Field(gt=0)
    exit_price: float = Field(gt=0)
    quantity: int = Field(gt=0)
    entry_timestamp: datetime
    exit_timestamp: datetime
    realized_pnl: float
    strategy_name: str | None = None
