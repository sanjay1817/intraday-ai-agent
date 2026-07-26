"""Automatic Paper Trading: risk controls (Requirement 7).

`AutoRiskManager` is a strategy-level gate the orchestrator consults
*before* ever attempting an entry — independent of, and in addition to,
whatever `app.paper.portfolio.PortfolioManager` itself already enforces
(cash sufficiency). It knows nothing about brokers, strategies, or
orders; it only tracks day-scoped counters and answers "is a new entry
currently allowed".

Two of Requirement 7's five controls live elsewhere by design, not
duplicated here:
- "Maximum open positions" needs the orchestrator's own live position
  count, not something this class tracks independently — passed in as
  a parameter to `check_entry_allowed` instead.
- "One position per symbol" is a per-symbol state check
  (`PaperTradingEngine.get_position`), not a day-scoped counter — the
  orchestrator checks it directly.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.auto.models import AutoTradingConfig

#: Matches `app.market.ingestion`'s own IST convention for "which
#: trading day is this" — day-scoped counters roll over at IST
#: midnight, not UTC midnight, since that's what "daily" means for an
#: NSE trading desk.
_INDIA_TZ = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True, slots=True)
class RiskCheckResult:
    """Whether a new entry is currently allowed, and why not if it isn't."""

    allowed: bool
    reason: str | None = None


class AutoRiskManager:
    """Tracks trades-today, consecutive losses, and an earned cooldown
    for one `AutoTradingConfig`; the day rolls over automatically the
    first time any method is called after IST midnight.
    """

    def __init__(self, config: AutoTradingConfig) -> None:
        self._config = config
        self._day: date | None = None
        self._trades_today = 0
        self._daily_realized_pnl = 0.0
        self._consecutive_losses = 0
        self._cooldown_until: datetime | None = None

    @property
    def trades_today(self) -> int:
        return self._trades_today

    @property
    def daily_realized_pnl(self) -> float:
        return self._daily_realized_pnl

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def cooldown_until(self) -> datetime | None:
        return self._cooldown_until

    def check_entry_allowed(
        self, *, open_position_count: int, now: datetime | None = None
    ) -> RiskCheckResult:
        """Whether a new entry may be attempted right now.

        Checks (in this order, so `reason` always names the first one
        actually hit): active cooldown, max open positions, max daily
        trades, max daily loss.
        """

        resolved_now = now or datetime.now(UTC)
        self._roll_day_if_needed(resolved_now)

        if self._cooldown_until is not None and resolved_now < self._cooldown_until:
            return RiskCheckResult(
                False, f"cooldown active until {self._cooldown_until.isoformat()}"
            )
        if open_position_count >= self._config.max_open_positions:
            return RiskCheckResult(
                False, f"max_open_positions ({self._config.max_open_positions}) reached"
            )
        if self._trades_today >= self._config.max_daily_trades:
            return RiskCheckResult(
                False, f"max_daily_trades ({self._config.max_daily_trades}) reached"
            )
        if self._daily_realized_pnl <= -self._config.max_daily_loss:
            return RiskCheckResult(
                False, f"max_daily_loss ({self._config.max_daily_loss}) breached"
            )
        return RiskCheckResult(True)

    def record_trade_opened(self, *, now: datetime | None = None) -> None:
        resolved_now = now or datetime.now(UTC)
        self._roll_day_if_needed(resolved_now)
        self._trades_today += 1

    def record_trade_closed(self, pnl: float, *, now: datetime | None = None) -> None:
        """Record a closed trade's realized P&L, updating the
        consecutive-loss streak and — once it reaches
        `cooldown_after_consecutive_losses` — starting a cooldown of
        `cooldown_minutes`.

        A profitable close resets the streak to 0 but never clears an
        already-active cooldown early; a cooldown is a fixed penalty
        once earned, not one a single subsequent win buys back.
        """

        resolved_now = now or datetime.now(UTC)
        self._roll_day_if_needed(resolved_now)
        self._daily_realized_pnl += pnl

        if pnl < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self._config.cooldown_after_consecutive_losses:
                self._cooldown_until = resolved_now + timedelta(
                    minutes=self._config.cooldown_minutes
                )
        else:
            self._consecutive_losses = 0

    def reset(self) -> None:
        """Clear every counter — backs `AutoTradingOrchestrator.start()`
        starting a fresh day's bookkeeping.
        """

        self._day = None
        self._trades_today = 0
        self._daily_realized_pnl = 0.0
        self._consecutive_losses = 0
        self._cooldown_until = None

    def _roll_day_if_needed(self, now: datetime) -> None:
        """Reset `trades_today`/`daily_realized_pnl` on an IST day
        change. Deliberately does NOT reset `consecutive_losses`/
        `cooldown_until`: a cooldown earned late one day is a real
        losing streak, not something that should evaporate at midnight
        while the position that caused it may still be open.
        """

        today = now.astimezone(_INDIA_TZ).date()
        if self._day != today:
            self._day = today
            self._trades_today = 0
            self._daily_realized_pnl = 0.0
