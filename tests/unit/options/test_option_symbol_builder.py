"""Unit tests for `OptionSymbolBuilder` (app.options.option_symbol_builder)."""

from datetime import date

import pytest

from app.options.models import OptionType
from app.options.option_symbol_builder import OptionSymbolBuilder


def test_build_matches_angel_one_index_option_symbol_convention() -> None:
    builder = OptionSymbolBuilder()

    symbol = builder.build("nifty", date(2026, 7, 28), 17500.0, OptionType.CE)

    assert symbol == "NIFTY28JUL202617500CE"


def test_build_rounds_strike_to_nearest_integer() -> None:
    builder = OptionSymbolBuilder()

    symbol = builder.build("BANKNIFTY", date(2026, 8, 6), 48250.6, OptionType.PE)

    assert symbol == "BANKNIFTY06AUG202648251PE"


def test_build_raises_for_non_positive_strike() -> None:
    builder = OptionSymbolBuilder()

    with pytest.raises(ValueError):
        builder.build("NIFTY", date(2026, 7, 28), 0, OptionType.CE)

    with pytest.raises(ValueError):
        builder.build("NIFTY", date(2026, 7, 28), -100, OptionType.CE)


def test_build_raises_for_blank_underlying() -> None:
    builder = OptionSymbolBuilder()

    with pytest.raises(ValueError):
        builder.build("   ", date(2026, 7, 28), 17500.0, OptionType.CE)
