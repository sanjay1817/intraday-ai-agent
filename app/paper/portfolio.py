"""Paper Trading Engine: the cash/equity/P&L ledger.

`PortfolioManager` is the single source of truth for cash, available
cash, used capital, equity, and P&L (realized, unrealized, and total).
It does *not* itself own position state — `app.paper.position_manager`
does, in a later milestone — so `snapshot()` takes the caller's current
positions as an argument and derives every position-dependent figure
(used capital, unrealized P&L, equity) from them fresh, every call,
rather than caching a copy that could drift out of sync. That split
keeps this class independently testable with no dependency on
`position_manager`, and it means `app.paper.models.Portfolio`'s own
invariants (see its docstring) are the actual arbiter of "is this
snapshot internally consistent" — `PortfolioManager` physically cannot
construct one that violates them, because it's the same formulas on
both sides.

Every public method is `async def`, even though this milestone's
implementation is pure in-memory arithmetic with no I/O: turning a sync
method into an async one later is a breaking change to a class's public
API, and Requirement 5 explicitly asks for a design PostgreSQL can back
later *without* changing that API. Starting async avoids ever having to
make that change. `app.brokers.BrokerInterface` sets the same precedent
elsewhere in this codebase — every method there is async "because every
implementation is I/O-bound", true of a real broker from day one and
intended to become true here too once a persistent ledger exists.

Cash movement (`debit`/`credit`) and realized-P&L tracking
(`record_realized_pnl`) are deliberately separate calls rather than one
"close a position" method: a closing SELL's cash proceeds and its
profit are two different numbers for two different purposes (`cash` is
real spendable money; realized P&L is a performance metric), and
keeping them separate here leaves `app.paper.position_manager` free to
decide exactly when and how to call each rather than this class
assuming a particular position-closing workflow.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from app.domain.exceptions.paper import InsufficientCashError
from app.paper.models import PaperPosition, Portfolio

#: Floating-point slack for cash comparisons (e.g. "is amount > available
#: cash") — matches the tolerance `app.paper.models.Portfolio`'s own
#: reconciliation validators use, so a value this class accepts as valid
#: never turns around and fails `Portfolio`'s construction.
_EPSILON = 1e-6


class PortfolioManager:
    """Owns cash, reserved cash, and realized P&L for one paper trading
    account, and composes `Portfolio` snapshots from that plus whatever
    positions the caller currently holds.
    """

    def __init__(self, initial_capital: float) -> None:
        self._validate_capital(initial_capital)
        self._initial_capital = initial_capital
        self._cash = initial_capital
        self._reserved_cash = 0.0
        self._realized_pnl = 0.0

    @property
    def initial_capital(self) -> float:
        return self._initial_capital

    @property
    def cash(self) -> float:
        """Actual spendable cash, after every fill's cost/proceeds."""

        return self._cash

    @property
    def reserved_cash(self) -> float:
        """Cash currently set aside against resting orders that haven't
        filled yet (see `reserve_cash`).
        """

        return self._reserved_cash

    @property
    def available_cash(self) -> float:
        """`cash` minus `reserved_cash` — what a new order can actually
        draw against right now.
        """

        return self._cash - self._reserved_cash

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    async def debit(self, amount: float) -> None:
        """Reduce cash by `amount` — a fill that costs money (e.g. a BUY).

        Raises:
            InsufficientCashError: `amount` exceeds `cash`.
        """

        self._check_non_negative("amount", amount)
        if amount > self._cash + _EPSILON:
            raise InsufficientCashError(
                f"paper: insufficient cash: requested {amount}, available {self._cash}",
                requested=amount,
                available=self._cash,
            )
        self._cash -= amount

    async def credit(self, amount: float) -> None:
        """Increase cash by `amount` — a fill that returns money (e.g. a SELL)."""

        self._check_non_negative("amount", amount)
        self._cash += amount

    async def reserve_cash(self, amount: float) -> None:
        """Set aside `amount` of `available_cash` against a resting
        order's worst-case cost — called when such an order is placed,
        so a second order can't over-commit capital the first has
        already claimed.

        Raises:
            InsufficientCashError: `amount` exceeds `available_cash`.
        """

        self._check_non_negative("amount", amount)
        if amount > self.available_cash + _EPSILON:
            raise InsufficientCashError(
                f"paper: insufficient available cash to reserve: requested {amount}, "
                f"available {self.available_cash}",
                requested=amount,
                available=self.available_cash,
            )
        self._reserved_cash += amount

    async def release_cash(self, amount: float) -> None:
        """Give back a previously `reserve_cash`d amount — called when
        the order it was reserved for fills (its actual cost is then
        `debit`ed separately) or is cancelled.

        Raises:
            ValueError: `amount` exceeds what is currently reserved —
                a caller-side bookkeeping bug, not a business rule.
        """

        self._check_non_negative("amount", amount)
        if amount > self._reserved_cash + _EPSILON:
            raise ValueError(
                f"cannot release {amount}: only {self._reserved_cash} is currently reserved"
            )
        self._reserved_cash -= amount

    async def record_realized_pnl(self, amount: float) -> None:
        """Add `amount` (positive for a profit, negative for a loss) to
        cumulative realized P&L.
        """

        self._realized_pnl += amount

    async def snapshot(
        self, positions: Sequence[PaperPosition], *, now: datetime | None = None
    ) -> Portfolio:
        """Compose the current `Portfolio` view from this ledger's cash/
        P&L state plus `positions` (as currently held by
        `app.paper.position_manager` — this class holds none of its own).
        """

        used_capital = sum(
            abs(position.quantity) * position.average_price for position in positions
        )
        unrealized_pnl = sum(position.unrealized_pnl for position in positions)
        market_value = sum(position.quantity * position.last_price for position in positions)

        return Portfolio(
            cash=self._cash,
            available_cash=self.available_cash,
            used_capital=used_capital,
            initial_capital=self._initial_capital,
            realized_pnl=self._realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_pnl=self._realized_pnl + unrealized_pnl,
            equity=self._cash + market_value,
            positions=list(positions),
            updated_at=now or datetime.now(UTC),
        )

    async def reset(self, initial_capital: float | None = None) -> None:
        """Reset this ledger to a fresh starting state — backs
        `POST /api/v1/paper/reset`. Defaults to this manager's current
        `initial_capital` (from construction, or from whichever `reset`
        call most recently overrode it); a caller may fund the reset
        account with a different amount instead, which itself becomes
        the new `initial_capital` for any future no-argument reset.
        """

        capital = initial_capital if initial_capital is not None else self._initial_capital
        self._validate_capital(capital)

        self._initial_capital = capital
        self._cash = capital
        self._reserved_cash = 0.0
        self._realized_pnl = 0.0

    @staticmethod
    def _validate_capital(capital: float) -> None:
        if capital <= 0:
            raise ValueError(f"initial_capital must be positive, got {capital}")

    @staticmethod
    def _check_non_negative(name: str, value: float) -> None:
        if value < 0:
            raise ValueError(f"{name} must be non-negative, got {value}")
