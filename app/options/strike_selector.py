"""Picks one strike from an underlying's available option strikes."""

from __future__ import annotations

from collections.abc import Sequence

from app.options.exceptions import StrikeNotFoundError
from app.options.models import OptionType, StrikeMode


class StrikeSelector:
    """Applies a `StrikeMode` to a list of available strikes.

    Moneyness direction reverses between calls and puts: for a call,
    strikes below the underlying's price are in-the-money and strikes
    above are out-of-the-money; for a put it's the reverse. `select()`
    accounts for this via `option_type` rather than requiring the caller
    to pre-compute a direction.
    """

    def __init__(self, default_mode: StrikeMode) -> None:
        self._default_mode = default_mode

    def select(
        self,
        available_strikes: Sequence[float],
        underlying_price: float,
        option_type: OptionType,
        *,
        mode: StrikeMode | None = None,
        steps: int = 1,
    ) -> float:
        """Return one strike from `available_strikes` per `mode`/`steps`.

        `steps` (>= 1) is how many strikes away from ATM to move for
        `ITM`/`OTM`; ignored for `ATM`. If `steps` would move past either
        end of the sorted, deduplicated strike list, the index is clamped
        to the nearest valid boundary rather than raising — a strike list
        that's shorter than requested (e.g. near a chain's edge) still
        returns the closest available strike instead of failing outright.

        Raises:
            StrikeNotFoundError: `available_strikes` is empty.
            ValueError: `steps < 1`.
        """

        if steps < 1:
            raise ValueError(f"steps must be >= 1, got {steps}")

        strikes = sorted(set(available_strikes))
        if not strikes:
            raise StrikeNotFoundError("no strikes available to select from")

        effective_mode = mode if mode is not None else self._default_mode
        atm_index = self._atm_index(strikes, underlying_price)

        if effective_mode is StrikeMode.ATM:
            return strikes[atm_index]

        moves_up = (effective_mode is StrikeMode.OTM and option_type is OptionType.CE) or (
            effective_mode is StrikeMode.ITM and option_type is OptionType.PE
        )
        raw_index = atm_index + steps if moves_up else atm_index - steps
        clamped_index = max(0, min(len(strikes) - 1, raw_index))
        return strikes[clamped_index]

    @staticmethod
    def _atm_index(strikes: list[float], underlying_price: float) -> int:
        """Index of the strike closest to `underlying_price`, ties broken
        toward the lower strike.
        """

        best_index = 0
        best_distance = abs(strikes[0] - underlying_price)
        for index in range(1, len(strikes)):
            distance = abs(strikes[index] - underlying_price)
            if distance < best_distance:
                best_index = index
                best_distance = distance
        return best_index
