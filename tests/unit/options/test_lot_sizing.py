"""Unit tests for `app.options.lot_sizing.resolve_lot_size`."""

from datetime import UTC, date, datetime

from app.domain.enums.trading import Exchange
from app.options.lot_sizing import resolve_lot_size
from app.options.models import OptionChain, OptionInstrument, OptionType

_EXPIRY = date(2026, 8, 7)


def _instrument(strike: float, option_type: OptionType, lot_size: int | None) -> OptionInstrument:
    return OptionInstrument(
        underlying="NIFTY",
        expiry=_EXPIRY,
        strike=strike,
        option_type=option_type,
        tradingsymbol=f"NIFTY07AUG2026{int(strike)}{option_type.value}",
        exchange=Exchange.NFO,
        token="1",
        lot_size=lot_size,
    )


def _chain(instruments: list[OptionInstrument]) -> OptionChain:
    return OptionChain(underlying="NIFTY", fetched_at=datetime.now(UTC), instruments=tuple(instruments))


def test_uses_chain_lot_size_when_instrument_found() -> None:
    chain = _chain([_instrument(24000.0, OptionType.CE, 75)])

    result = resolve_lot_size(
        chain, expiry=_EXPIRY, strike=24000.0, option_type=OptionType.CE, default_lot_size=50
    )

    assert result == 75


def test_falls_back_to_default_when_instrument_not_found() -> None:
    chain = _chain([_instrument(24000.0, OptionType.CE, 75)])

    result = resolve_lot_size(
        chain, expiry=_EXPIRY, strike=24500.0, option_type=OptionType.CE, default_lot_size=50
    )

    assert result == 50


def test_falls_back_to_default_when_instrument_lot_size_is_none() -> None:
    chain = _chain([_instrument(24000.0, OptionType.CE, None)])

    result = resolve_lot_size(
        chain, expiry=_EXPIRY, strike=24000.0, option_type=OptionType.CE, default_lot_size=50
    )

    assert result == 50
