"""Automatic Paper Trading: configuration and status models."""

from datetime import datetime, time
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums.trading import Exchange, HistoricalInterval

#: 10 minutes before NSE's official 15:30 IST close — a deliberate
#: safety buffer, not the close time itself, so a square-off order has
#: time to actually execute before the session ends.
_DEFAULT_SQUARE_OFF_TIME = time(15, 20)


class AutoTradingConfig(BaseModel):
    """Every knob `app.auto.orchestrator.AutoTradingOrchestrator` runs
    against. Constructed once from `app.config.settings.Settings` at
    application startup (see `app.main`'s lifespan).
    """

    model_config = ConfigDict(frozen=True)

    symbols: list[str] = Field(default_factory=list)
    #: All configured symbols trade on the same exchange — this
    #: milestone's `AUTO_SYMBOLS` is a flat list of trading symbols
    #: (e.g. `["SBIN-EQ", "RELIANCE-EQ"]`), not exchange-qualified
    #: pairs, matching every example elsewhere in this project so far.
    exchange: Exchange = Exchange.NSE
    interval: HistoricalInterval = HistoricalInterval.FIVE_MINUTE
    confidence_threshold: float = Field(default=60.0, ge=0, le=100)
    max_open_positions: int = Field(default=3, gt=0)
    max_daily_trades: int = Field(default=10, gt=0)
    max_daily_loss: float = Field(default=5000.0, gt=0)
    #: Fraction of *available* (not total) cash risked on one new
    #: entry's cost — not itself one of Requirement 10's named
    #: settings, but a position-sizing rule has to come from
    #: somewhere; documented here rather than left as a hidden
    #: constant. Available cash (not total) so a resting order's
    #: reservation is respected too.
    capital_fraction_per_trade: float = Field(default=0.1, gt=0, le=1)
    poll_interval_seconds: float = Field(default=30.0, gt=0)
    cooldown_after_consecutive_losses: int = Field(default=3, gt=0)
    cooldown_minutes: float = Field(default=30.0, gt=0)
    #: IST wall-clock time at/after which every open auto-managed
    #: position is force-exited regardless of any other condition.
    square_off_time: time = _DEFAULT_SQUARE_OFF_TIME


class AutoTradingStatus(BaseModel):
    """`GET /api/v1/auto/status`'s response — a point-in-time snapshot
    of `AutoTradingOrchestrator`'s own state, not derived from
    `PaperTradingEngine` (whose own portfolio/positions/trades are
    already served by `GET /api/v1/paper/*`).
    """

    model_config = ConfigDict(frozen=True)

    running: bool
    started_at: datetime | None = None
    symbols: list[str] = Field(default_factory=list)
    cycle_count: Annotated[int, Field(ge=0)] = 0
    last_cycle_at: datetime | None = None
    open_position_count: Annotated[int, Field(ge=0)] = 0
    trades_today: Annotated[int, Field(ge=0)] = 0
    daily_realized_pnl: float = 0.0
    consecutive_losses: Annotated[int, Field(ge=0)] = 0
    cooldown_until: datetime | None = None
    last_error: str | None = None
