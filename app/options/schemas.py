"""Pydantic DTOs mirroring `app.options.models`, for future API exposure.

Not wired to any router this phase — kept here so a later phase's
option-chain endpoint has a ready-made response shape rather than
inventing one ad hoc.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums.trading import Exchange
from app.options.models import OptionChain, OptionInstrument, OptionType


class OptionInstrumentSchema(BaseModel):
    """API-facing mirror of `app.options.models.OptionInstrument`."""

    model_config = ConfigDict(from_attributes=True)

    underlying: str
    expiry: date
    strike: float
    option_type: OptionType
    tradingsymbol: str
    exchange: Exchange
    token: str | None = None
    lot_size: int | None = None

    @classmethod
    def from_domain(cls, instrument: OptionInstrument) -> OptionInstrumentSchema:
        return cls.model_validate(instrument)


class OptionChainSchema(BaseModel):
    """API-facing mirror of `app.options.models.OptionChain`."""

    model_config = ConfigDict(from_attributes=True)

    underlying: str
    fetched_at: datetime
    instruments: list[OptionInstrumentSchema]

    @classmethod
    def from_domain(cls, chain: OptionChain) -> OptionChainSchema:
        return cls(
            underlying=chain.underlying,
            fetched_at=chain.fetched_at,
            instruments=[OptionInstrumentSchema.from_domain(i) for i in chain.instruments],
        )


class OptionSignal(StrEnum):
    """Bullish/bearish/no-trade call at the options package's own API
    boundary.

    Deliberately distinct from `app.strategy.models.SignalDirection`
    (`BULLISH`/`BEARISH`/`NONE`) rather than reusing it directly, the same
    way `app.instructor.recommendation.RecommendationAction` is
    deliberately narrower than `app.ai.decision_models.TradingAction` per
    that file's own docstring: this is the vocabulary
    `OptionRecommendation` — this package's own response shape — speaks,
    independent of whichever strategy/instructor vocabulary happens to
    produce it upstream. `NO_TRADE` (rather than `NONE`) is deliberately
    named for what it means to a caller of this API: don't open an
    option position.
    """

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NO_TRADE = "NO_TRADE"


class OptionRecommendation(BaseModel):
    """One underlying's actionable option-contract recommendation — the
    output of `app.options.recommendation.generate_option_recommendation`.

    Frozen, like `InstructorRecommendation`: a point-in-time analysis
    result, not a mutable object. `tradingsymbol`/`expiry`/`strike`/
    `option_type`/`premium` are all `None` together when `signal` is
    `NO_TRADE` (nothing to trade) and all set together otherwise — see
    the validator below.
    """

    model_config = ConfigDict(frozen=True)

    underlying: str = Field(min_length=1)
    signal: OptionSignal
    tradingsymbol: str | None = None
    expiry: date | None = None
    strike: float | None = None
    option_type: OptionType | None = None
    premium: float | None = None
    underlying_ltp: float
    confidence: float = Field(ge=0, le=100)
    reasoning: str = Field(min_length=1)
    generated_at: datetime

    @model_validator(mode="after")
    def _check_option_fields_are_consistent_with_signal(self) -> OptionRecommendation:
        """The same "internally coherent output" check
        `InstructorRecommendation`/`StrategySignal` apply to their own
        directional fields, applied here to the presence/absence of the
        contract-specific fields instead of price levels: `NO_TRADE`
        means there is no contract to describe, so every contract field
        must be `None`; `BULLISH`/`BEARISH` means a contract was
        selected, so every contract field must be set.
        """

        contract_fields = (
            self.tradingsymbol,
            self.expiry,
            self.strike,
            self.option_type,
            self.premium,
        )

        if self.signal is OptionSignal.NO_TRADE:
            if any(field is not None for field in contract_fields):
                raise ValueError(
                    "NO_TRADE requires tradingsymbol/expiry/strike/option_type/premium "
                    "to all be None"
                )
        else:
            if any(field is None for field in contract_fields):
                raise ValueError(
                    f"{self.signal} requires tradingsymbol/expiry/strike/option_type/premium "
                    "to all be set"
                )

        return self


class OptionExitReason(StrEnum):
    """Why a Phase 3 paper option position was closed.

    `TARGET`/`STOP_LOSS` are the two bracket exit legs
    `app.paper.order_manager._maybe_spawn_bracket_children` already
    auto-spawns from an entry order's `stop_loss_price`/`target_price` —
    this enum only *labels* which one closed a position after the fact
    (via the closing order's own `tag`, see
    `app.options.paper_trading.get_option_trade_history`); it triggers no
    exit logic of its own. `MANUAL` is the one case
    `app.options.paper_trading.exit_option_position` actively drives.
    """

    MANUAL = "MANUAL"
    TARGET = "TARGET"
    STOP_LOSS = "STOP_LOSS"


class OptionTradeHistoryEntry(BaseModel):
    """One closed option round-trip, enriched for `GET
    /api/v1/options/paper/trades` — a read-side view over
    `app.paper.models.ClosedTrade`, not a new model the Paper Trading
    Engine itself produces (see `app.options.paper_trading`'s module
    docstring for why no new trade-history storage exists here).
    """

    model_config = ConfigDict(frozen=True)

    trade_id: str = Field(min_length=1)
    underlying: str = Field(min_length=1)
    tradingsymbol: str = Field(min_length=1)
    entry_premium: float = Field(gt=0)
    exit_premium: float = Field(gt=0)
    pnl: float
    holding_seconds: float = Field(ge=0)
    reason: str | None = None
    confidence: float | None = None
    reasoning: str | None = None
    entry_timestamp: datetime
    exit_timestamp: datetime


class OptionRiskStatus(BaseModel):
    """`GET /api/v1/options/risk/status`'s response: a read-only snapshot
    of the Phase 3 options risk gate's (`app.options.risk.OptionRiskManager`)
    current counters and the configured limits they're measured against —
    the Phase 4 Angular dashboard's "Risk Panel" section reads this
    directly rather than re-deriving any of it client-side.
    """

    model_config = ConfigDict(frozen=True)

    current_premium_exposure: float
    max_premium_exposure: float
    remaining_premium_capacity: float
    daily_realized_pnl: float
    max_daily_loss: float
    max_lots_per_order: int
    max_premium_per_order: float
