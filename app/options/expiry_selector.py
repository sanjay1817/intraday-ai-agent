"""Picks one expiry from an underlying's available option expiries."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from app.options.exceptions import ExpiryNotFoundError
from app.options.models import ExpiryMode


class ExpirySelector:
    """Applies an `ExpiryMode` to a list of available expiries.

    Stateless beyond `default_mode` (the mode used when `select()`'s
    `mode` argument is omitted) — every call is otherwise a pure function
    of its arguments.
    """

    def __init__(self, default_mode: ExpiryMode) -> None:
        self._default_mode = default_mode

    def select(
        self,
        expiries: Sequence[date],
        *,
        mode: ExpiryMode | None = None,
        reference_date: date | None = None,
    ) -> date:
        """Return one expiry from `expiries` per `mode` (or `default_mode`).

        `reference_date` (defaults to `date.today()`) is the "as of" date:
        only expiries on or after it are considered, so an already-expired
        contract still present in a stale chain is never selected.

        Raises:
            ExpiryNotFoundError: `expiries` is empty, no expiry is on or
                after `reference_date`, or the requested mode can't be
                satisfied (e.g. `NEXT_WEEKLY` with fewer than two upcoming
                expiries).
        """

        effective_mode = mode if mode is not None else self._default_mode
        as_of = reference_date if reference_date is not None else date.today()

        upcoming = sorted(expiry for expiry in expiries if expiry >= as_of)
        if not upcoming:
            raise ExpiryNotFoundError(
                f"no expiry on or after {as_of.isoformat()} available for mode {effective_mode.value}"
            )

        if effective_mode is ExpiryMode.NEAREST_WEEKLY:
            return upcoming[0]

        if effective_mode is ExpiryMode.NEXT_WEEKLY:
            if len(upcoming) < 2:
                raise ExpiryNotFoundError(
                    "NEXT_WEEKLY requires at least two upcoming expiries, "
                    f"only {len(upcoming)} available"
                )
            return upcoming[1]

        # MONTHLY: group upcoming expiries by (year, month) and take the
        # latest date in each group (the month's final/monthly contract),
        # then return the earliest such monthly date.
        latest_by_month: dict[tuple[int, int], date] = {}
        for expiry in upcoming:
            key = (expiry.year, expiry.month)
            current = latest_by_month.get(key)
            if current is None or expiry > current:
                latest_by_month[key] = expiry
        return min(latest_by_month.values())
