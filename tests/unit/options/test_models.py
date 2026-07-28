"""Unit tests for `OptionChain`'s helper methods (app.options.models)."""

from datetime import date, datetime

from app.domain.enums.trading import Exchange
from app.options.models import OptionChain, OptionInstrument, OptionType


def _instrument(expiry: date, strike: float, option_type: OptionType) -> OptionInstrument:
    return OptionInstrument(
        underlying="NIFTY",
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        tradingsymbol=f"NIFTY{expiry.strftime('%d%b%Y').upper()}{int(strike)}{option_type.value}",
        exchange=Exchange.NFO,
        token="123",
        lot_size=50,
    )


def _chain(instruments: list[OptionInstrument]) -> OptionChain:
    return OptionChain(
        underlying="NIFTY", fetched_at=datetime(2026, 7, 28, 9, 0), instruments=tuple(instruments)
    )


def test_expiries_returns_sorted_unique_dates() -> None:
    e1, e2 = date(2026, 7, 30), date(2026, 8, 6)
    chain = _chain(
        [
            _instrument(e2, 24000, OptionType.CE),
            _instrument(e1, 24000, OptionType.CE),
            _instrument(e1, 24100, OptionType.PE),
        ]
    )

    assert chain.expiries() == (e1, e2)


def test_strikes_for_expiry_returns_sorted_unique_strikes_for_that_expiry_only() -> None:
    e1, e2 = date(2026, 7, 30), date(2026, 8, 6)
    chain = _chain(
        [
            _instrument(e1, 24100, OptionType.CE),
            _instrument(e1, 24000, OptionType.PE),
            _instrument(e2, 25000, OptionType.CE),
        ]
    )

    assert chain.strikes_for_expiry(e1) == (24000.0, 24100.0)


def test_strikes_for_expiry_with_no_match_returns_empty_tuple() -> None:
    chain = _chain([_instrument(date(2026, 7, 30), 24000, OptionType.CE)])

    assert chain.strikes_for_expiry(date(2099, 1, 1)) == ()


def test_instrument_for_returns_matching_instrument() -> None:
    e1 = date(2026, 7, 30)
    target = _instrument(e1, 24000, OptionType.CE)
    chain = _chain([target, _instrument(e1, 24000, OptionType.PE)])

    result = chain.instrument_for(e1, 24000, OptionType.CE)

    assert result is target


def test_instrument_for_returns_none_when_not_found() -> None:
    chain = _chain([_instrument(date(2026, 7, 30), 24000, OptionType.CE)])

    assert chain.instrument_for(date(2026, 7, 30), 25000, OptionType.CE) is None
