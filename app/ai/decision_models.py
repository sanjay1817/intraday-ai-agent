"""The AI Decision Engine's entire input/output contract.

`MarketContext` (and the DTOs it is built from) is everything the AI is
allowed to see — a plain, framework-agnostic-except-for-Pydantic snapshot,
never a raw ORM model or a live broker/database handle. `TradingDecision`
is everything the AI is allowed to produce. Per this project's core
invariant, the AI Decision Engine never talks to a broker: this module is
therefore also the *entire* boundary between `app.ai` and the rest of the
application.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums.trading import Exchange, OrderSide
from app.indicators.schemas import IndicatorResult


class TradingAction(StrEnum):
    """Every decision the AI Decision Engine is allowed to return."""

    BUY = "BUY"
    SELL = "SELL"
    EXIT = "EXIT"
    HOLD = "HOLD"
    NO_TRADE = "NO_TRADE"


#: The actions that represent a new entry a caller could actually place —
#: the ones `app.ai.validator`'s confidence threshold gates.
ACTIONABLE_ENTRY_ACTIONS = frozenset({TradingAction.BUY, TradingAction.SELL})


class TradingDecision(BaseModel):
    """The AI Decision Engine's entire output surface.

    Frozen: once parsed and validated, a decision is a historical record.
    `app.ai.validator` returns a *new* `TradingDecision` when a policy
    rejects one (e.g. confidence below threshold) rather than mutating it.
    """

    model_config = ConfigDict(frozen=True)

    action: TradingAction
    confidence: float = Field(ge=0, le=100)
    entry_price: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    target_1: float | None = Field(default=None, gt=0)
    target_2: float | None = Field(default=None, gt=0)
    position_size: int | None = Field(default=None, ge=0)
    risk_reward_ratio: float | None = Field(default=None, ge=0)
    reasoning: str = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)
    timestamp: datetime

    @model_validator(mode="after")
    def _check_price_levels_are_directionally_consistent(self) -> "TradingDecision":
        """Reject an internally-incoherent trade plan at parse time.

        A BUY's stop must sit below its entry and both targets above it;
        a SELL is the mirror image. This is "invalid stop loss"/"invalid
        targets" from the parsing contract — structural correctness, not
        a confidence/risk policy (that's `app.ai.validator`'s job).
        """

        if self.action not in ACTIONABLE_ENTRY_ACTIONS:
            return self

        if self.entry_price is None or self.stop_loss is None:
            raise ValueError(f"{self.action} requires both entry_price and stop_loss")

        targets = [target for target in (self.target_1, self.target_2) if target is not None]

        if self.action is TradingAction.BUY:
            if not self.stop_loss < self.entry_price:
                raise ValueError("BUY requires stop_loss below entry_price")
            if any(target <= self.entry_price for target in targets):
                raise ValueError("BUY requires targets above entry_price")
            if (
                self.target_1 is not None
                and self.target_2 is not None
                and self.target_2 <= self.target_1
            ):
                raise ValueError("BUY requires target_2 above target_1")
        else:  # TradingAction.SELL
            if not self.stop_loss > self.entry_price:
                raise ValueError("SELL requires stop_loss above entry_price")
            if any(target >= self.entry_price for target in targets):
                raise ValueError("SELL requires targets below entry_price")
            if (
                self.target_1 is not None
                and self.target_2 is not None
                and self.target_2 >= self.target_1
            ):
                raise ValueError("SELL requires target_2 below target_1")

        return self


class Candle(BaseModel):
    """One OHLCV bar, as shown to the AI — never a raw ORM row."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int = Field(ge=0)


class MarketSentimentLabel(StrEnum):
    """A coarse sentiment reading for the symbol under analysis."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class MarketSentiment(BaseModel):
    """A sentiment snapshot, from whatever upstream source produced it."""

    model_config = ConfigDict(frozen=True)

    label: MarketSentimentLabel
    score: float | None = Field(default=None, ge=-1.0, le=1.0)
    source: str | None = None


class OpenPosition(BaseModel):
    """One currently-open position, as shown to the AI."""

    model_config = ConfigDict(frozen=True)

    tradingsymbol: str
    exchange: Exchange
    side: OrderSide
    quantity: int = Field(gt=0)
    entry_price: float = Field(gt=0)
    current_price: float = Field(gt=0)
    unrealized_pnl: float


class RiskSettings(BaseModel):
    """The account's configured risk limits, as shown to the AI.

    The AI reasons within these; it never sets or changes them — they
    come from `app.config.settings`, not from the AI's own output.
    """

    model_config = ConfigDict(frozen=True)

    max_position_size: int = Field(gt=0)
    max_daily_loss: float = Field(gt=0)
    max_exposure: float = Field(gt=0)
    risk_per_trade_percent: float = Field(gt=0, le=100)


class MarketContext(BaseModel):
    """Everything the AI Decision Engine is allowed to see for one
    analysis.

    `app.ai.prompt_builder.PromptBuilder` renders this into a prompt, and
    this — not a database session, not an ORM model, not a live broker
    handle — is the only input the rest of `app.ai` ever touches.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    timeframe: str = Field(min_length=1)
    candles: list[Candle] = Field(min_length=1)
    indicators: dict[str, IndicatorResult[Any]] = Field(default_factory=dict)
    sentiment: MarketSentiment | None = None
    open_positions: list[OpenPosition] = Field(default_factory=list)
    available_capital: float = Field(ge=0)
    current_exposure: float = Field(ge=0)
    todays_pnl: float
    risk_settings: RiskSettings
