"""Risk Management Engine input DTOs.

`RiskValidationRequest` is the sole argument `app.risk.engine.RiskEngine`
accepts — everything needed to approve, size, adjust, or reject one
candidate trade, assembled by the caller (the orchestration layer that
already holds a `StrategySignal`, a `TradingDecision`, and current
account/broker state). Consistent with every other engine in this
project, the Risk Engine never fetches this data itself.

Reuses `StrategySignal` (`app.strategy.models`), `TradingDecision`
(`app.ai.decision_models`), `Position`/`OrderDetail`/`BrokerFunds`
(`app.domain.entities.broker`), and `MarketSessionState`
(`app.market.dto`) rather than re-modeling data every upstream engine
already produces.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.ai.decision_models import TradingDecision
from app.domain.entities.broker import BrokerFunds, OrderDetail, Position
from app.market.dto import MarketSessionState
from app.strategy.models import StrategySignal


class PositionSizingMethod(StrEnum):
    """How `app.risk.position_sizer` should size a new position."""

    FIXED_QUANTITY = "FIXED_QUANTITY"
    FIXED_CAPITAL = "FIXED_CAPITAL"
    PERCENTAGE_RISK = "PERCENTAGE_RISK"
    ATR_BASED = "ATR_BASED"
    VOLATILITY_BASED = "VOLATILITY_BASED"
    KELLY_CRITERION = "KELLY_CRITERION"


class RiskConfiguration(BaseModel):
    """Every configurable risk threshold this engine enforces.

    No field has a business-meaningful hard-coded default baked into
    engine logic — thresholds live here, sourced from
    `app.config.settings` by the caller, so tuning a limit is a
    configuration change, never a code change.
    """

    model_config = ConfigDict(frozen=True)

    # -- Position sizing --------------------------------------------------
    position_sizing_method: PositionSizingMethod = PositionSizingMethod.PERCENTAGE_RISK
    fixed_quantity: int | None = Field(default=None, gt=0)
    fixed_capital_amount: float | None = Field(default=None, gt=0)
    risk_per_trade_percent: float = Field(gt=0, le=100)
    atr_multiplier: float = Field(default=1.5, gt=0)
    kelly_fraction_cap: float = Field(default=0.5, gt=0, le=1.0)

    # -- Loss limits -------------------------------------------------------
    max_daily_loss: float = Field(gt=0)
    max_weekly_loss: float = Field(gt=0)
    max_monthly_loss: float = Field(gt=0)

    # -- Position / exposure limits ----------------------------------------
    max_open_positions: int = Field(gt=0)
    max_exposure_per_symbol: float = Field(gt=0)
    max_exposure_per_sector: float = Field(gt=0)
    max_total_exposure: float = Field(gt=0)

    # -- Broker / leverage limits -------------------------------------------
    max_leverage: float = Field(gt=0)
    max_order_value: float = Field(gt=0)

    # -- Trade quality / frequency limits ------------------------------------
    min_risk_reward: float = Field(gt=0)
    max_daily_trades: int = Field(gt=0)
    max_consecutive_losses: int = Field(gt=0)
    cooldown_minutes_after_loss_streak: int = Field(gt=0)

    # -- Volatility / session -------------------------------------------------
    min_atr_percent: float = Field(default=0.0, ge=0)
    allowed_sessions: frozenset[MarketSessionState] = Field(
        default_factory=lambda: frozenset({MarketSessionState.OPEN})
    )


class PositionExposure(BaseModel):
    """One open position plus the sector classification
    `app.risk.exposure_manager` needs for per-sector exposure limits.

    `Position` (`app.domain.entities.broker`) doesn't carry a sector tag
    — it's a broker concept, not a risk one — so this wraps rather than
    modifies it.
    """

    model_config = ConfigDict(frozen=True)

    position: Position
    sector: str | None = None

    @property
    def exposure_value(self) -> float:
        """Notional exposure of this position at its last traded price."""

        return abs(self.position.quantity) * self.position.last_price


class RiskValidationRequest(BaseModel):
    """Everything `app.risk` needs to evaluate one candidate trade."""

    model_config = ConfigDict(frozen=True)

    signal: StrategySignal
    decision: TradingDecision
    current_positions: list[PositionExposure] = Field(default_factory=list)
    open_orders: list[OrderDetail] = Field(default_factory=list)
    available_capital: float = Field(ge=0)
    todays_pnl: float
    weekly_pnl: float
    monthly_pnl: float
    broker_funds: BrokerFunds
    consecutive_losses: int = Field(default=0, ge=0)
    trades_today: int = Field(default=0, ge=0)
    current_session: MarketSessionState
    current_atr_percent: float | None = Field(default=None, ge=0)
    config: RiskConfiguration
