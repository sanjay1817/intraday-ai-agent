"""Phase 3: an independent, options-only risk gate for paper option orders.

Mirrors `app.auto.risk.AutoRiskManager`'s *shape* (a day-scoped,
IST-midnight-rollover realized-P&L counter gating new entries) but is a
fully independent class with no import from `app.auto` — Auto Trading
integration is explicitly out of scope for this phase (see
`docs/OPTIONS_PHASE3.md`'s Risk Model section), and mirroring the
~10-line day-rollover helper locally is cheaper than coupling this
package to Auto Trading's module boundary for it.

There is also no wired-in, generic `RiskEngine` in this codebase to
extend instead: `app/risk/models.py`/`app/risk/dto.py` exist, but no
`app/risk/engine.py` — nothing in `app.risk` is actually constructed or
consulted by the running application today. `OptionRiskManager` is
therefore a new, self-contained module, not an extension of an existing
one; it knows nothing about brokers, strategies, or the Paper Trading
Engine, exactly as `AutoRiskManager` knows nothing about them — it only
ever sees lot counts, premium values, and realized P&L numbers the
caller supplies.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

#: Matches `app.auto.risk`'s own IST convention for "which trading day is
#: this" — day-scoped counters roll over at IST midnight, not UTC
#: midnight, since that's what "daily" means for an NSE trading desk.
_INDIA_TZ = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True, slots=True)
class OptionRiskCheckResult:
    """Whether a candidate option order is currently allowed, and why not
    if it isn't.
    """

    allowed: bool
    reason: str | None = None


class OptionRiskManager:
    """Tracks today's realized option P&L and gates new paper option
    orders against a fixed set of per-order/exposure limits.

    The day rolls over automatically the first time any method observes
    a `now` past IST midnight — the same lazy-rollover design
    `AutoRiskManager` uses, so there is no background scheduler to keep
    the day boundary honest.
    """

    def __init__(
        self,
        *,
        max_lots_per_order: int,
        max_premium_per_order: float,
        max_premium_exposure: float,
        max_daily_loss: float,
    ) -> None:
        self._max_lots_per_order = max_lots_per_order
        self._max_premium_per_order = max_premium_per_order
        self._max_premium_exposure = max_premium_exposure
        self._max_daily_loss = max_daily_loss
        self._day: date | None = None
        self._daily_realized_pnl = 0.0

    @property
    def daily_realized_pnl(self) -> float:
        return self._daily_realized_pnl

    def check_order_allowed(
        self,
        *,
        lots: int,
        order_premium_value: float,
        current_premium_exposure: float,
        now: datetime | None = None,
    ) -> OptionRiskCheckResult:
        """Whether a candidate order is currently allowed.

        Checks (in this order, so `reason` always names the first one
        actually hit):
        1. an active daily-loss lockout (today's realized option P&L
           already at or below `-max_daily_loss`);
        2. `lots` exceeding `max_lots_per_order`;
        3. `order_premium_value` exceeding `max_premium_per_order`;
        4. `current_premium_exposure + order_premium_value` exceeding
           `max_premium_exposure`.
        """

        resolved_now = now or datetime.now(UTC)
        self._roll_day_if_needed(resolved_now)

        if self._daily_realized_pnl <= -self._max_daily_loss:
            return OptionRiskCheckResult(
                False, f"option_max_daily_loss ({self._max_daily_loss}) breached"
            )
        if lots > self._max_lots_per_order:
            return OptionRiskCheckResult(
                False, f"option_max_lots_per_order ({self._max_lots_per_order}) exceeded"
            )
        if order_premium_value > self._max_premium_per_order:
            return OptionRiskCheckResult(
                False,
                f"option_max_premium_per_order ({self._max_premium_per_order}) exceeded",
            )
        if current_premium_exposure + order_premium_value > self._max_premium_exposure:
            return OptionRiskCheckResult(
                False,
                f"option_max_premium_exposure ({self._max_premium_exposure}) exceeded",
            )
        return OptionRiskCheckResult(True)

    def record_trade_closed(self, pnl: float, *, now: datetime | None = None) -> None:
        """Accumulate today's realized option P&L (positive for a
        profit, negative for a loss).
        """

        resolved_now = now or datetime.now(UTC)
        self._roll_day_if_needed(resolved_now)
        self._daily_realized_pnl += pnl

    def reset(self) -> None:
        """Clear every counter."""

        self._day = None
        self._daily_realized_pnl = 0.0

    def _roll_day_if_needed(self, now: datetime) -> None:
        """Reset `daily_realized_pnl` on an IST day change — the same
        rollover rule `app.auto.risk.AutoRiskManager._roll_day_if_needed`
        applies, re-derived locally rather than imported (see this
        module's docstring).
        """

        today = now.astimezone(_INDIA_TZ).date()
        if self._day != today:
            self._day = today
            self._daily_realized_pnl = 0.0
