"""Unit tests for `app.options.schemas.OptionRecommendation`'s
NO_TRADE/BULLISH/BEARISH field-consistency validator.
"""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from app.options.models import OptionType
from app.options.schemas import OptionRecommendation, OptionSignal

_NOW = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
_EXPIRY = date(2026, 8, 7)


def _base_kwargs(**overrides: object) -> dict:
    kwargs: dict = {
        "underlying": "NIFTY",
        "signal": OptionSignal.NO_TRADE,
        "underlying_ltp": 24000.0,
        "confidence": 55.0,
        "reasoning": "HOLD: no strategy found a qualifying setup.",
        "generated_at": _NOW,
    }
    kwargs.update(overrides)
    return kwargs


def test_no_trade_with_all_option_fields_none_is_valid() -> None:
    rec = OptionRecommendation(**_base_kwargs())

    assert rec.signal == OptionSignal.NO_TRADE
    assert rec.tradingsymbol is None


def test_no_trade_with_a_populated_option_field_raises() -> None:
    with pytest.raises(ValidationError):
        OptionRecommendation(**_base_kwargs(strike=24000.0))


def test_bullish_with_all_fields_set_is_valid() -> None:
    rec = OptionRecommendation(
        **_base_kwargs(
            signal=OptionSignal.BULLISH,
            tradingsymbol="NIFTY07AUG202624000CE",
            expiry=_EXPIRY,
            strike=24000.0,
            option_type=OptionType.CE,
            premium=120.5,
        )
    )

    assert rec.signal == OptionSignal.BULLISH
    assert rec.tradingsymbol == "NIFTY07AUG202624000CE"


@pytest.mark.parametrize(
    "missing_field", ["tradingsymbol", "expiry", "strike", "option_type", "premium"]
)
def test_bullish_with_a_missing_field_raises(missing_field: str) -> None:
    kwargs = _base_kwargs(
        signal=OptionSignal.BULLISH,
        tradingsymbol="NIFTY07AUG202624000CE",
        expiry=_EXPIRY,
        strike=24000.0,
        option_type=OptionType.CE,
        premium=120.5,
    )
    kwargs[missing_field] = None

    with pytest.raises(ValidationError):
        OptionRecommendation(**kwargs)


def test_bearish_with_all_fields_set_is_valid() -> None:
    rec = OptionRecommendation(
        **_base_kwargs(
            signal=OptionSignal.BEARISH,
            tradingsymbol="NIFTY07AUG202624000PE",
            expiry=_EXPIRY,
            strike=24000.0,
            option_type=OptionType.PE,
            premium=98.0,
        )
    )

    assert rec.signal == OptionSignal.BEARISH
    assert rec.option_type == OptionType.PE
