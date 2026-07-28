"""Unit tests for `StrikeSelector` (app.options.strike_selector)."""

import pytest

from app.options.exceptions import StrikeNotFoundError
from app.options.models import OptionType, StrikeMode
from app.options.strike_selector import StrikeSelector

STRIKES = [23800, 23900, 24000, 24100, 24200, 24300, 24400]


def test_atm_returns_closest_strike() -> None:
    selector = StrikeSelector(StrikeMode.ATM)

    result = selector.select(STRIKES, 24050, OptionType.CE)

    assert result == 24000


def test_atm_ties_broken_toward_lower_strike() -> None:
    selector = StrikeSelector(StrikeMode.ATM)

    # Exactly between 24000 and 24100.
    result = selector.select(STRIKES, 24050.0, OptionType.CE)

    assert result == 24000


def test_atm_ignores_steps() -> None:
    selector = StrikeSelector(StrikeMode.ATM)

    result = selector.select(STRIKES, 24000, OptionType.CE, steps=3)

    assert result == 24000


def test_ce_itm_is_below_atm() -> None:
    selector = StrikeSelector(StrikeMode.ITM)

    result = selector.select(STRIKES, 24000, OptionType.CE, steps=1)

    assert result == 23900


def test_ce_otm_is_above_atm() -> None:
    selector = StrikeSelector(StrikeMode.OTM)

    result = selector.select(STRIKES, 24000, OptionType.CE, steps=1)

    assert result == 24100


def test_pe_itm_is_above_atm() -> None:
    selector = StrikeSelector(StrikeMode.ITM)

    result = selector.select(STRIKES, 24000, OptionType.PE, steps=1)

    assert result == 24100


def test_pe_otm_is_below_atm() -> None:
    selector = StrikeSelector(StrikeMode.OTM)

    result = selector.select(STRIKES, 24000, OptionType.PE, steps=1)

    assert result == 23900


def test_steps_beyond_upper_boundary_clamps_to_last_strike() -> None:
    selector = StrikeSelector(StrikeMode.OTM)

    result = selector.select(STRIKES, 24000, OptionType.CE, steps=10)

    assert result == 24400


def test_steps_beyond_lower_boundary_clamps_to_first_strike() -> None:
    selector = StrikeSelector(StrikeMode.ITM)

    result = selector.select(STRIKES, 24000, OptionType.CE, steps=10)

    assert result == 23800


def test_select_dedupes_available_strikes() -> None:
    selector = StrikeSelector(StrikeMode.ATM)

    result = selector.select([24000, 24000, 24100], 24000, OptionType.CE)

    assert result == 24000


def test_select_raises_on_empty_strikes() -> None:
    selector = StrikeSelector(StrikeMode.ATM)

    with pytest.raises(StrikeNotFoundError):
        selector.select([], 24000, OptionType.CE)


def test_select_raises_on_invalid_steps() -> None:
    selector = StrikeSelector(StrikeMode.ITM)

    with pytest.raises(ValueError):
        selector.select(STRIKES, 24000, OptionType.CE, steps=0)


def test_select_uses_default_mode_when_mode_omitted() -> None:
    selector = StrikeSelector(StrikeMode.OTM)

    result = selector.select(STRIKES, 24000, OptionType.CE)

    assert result == 24100


def test_select_mode_argument_overrides_default_mode() -> None:
    selector = StrikeSelector(StrikeMode.OTM)

    result = selector.select(STRIKES, 24000, OptionType.CE, mode=StrikeMode.ITM)

    assert result == 23900
