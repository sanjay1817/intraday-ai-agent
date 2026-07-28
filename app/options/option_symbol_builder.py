"""Builds an Angel-One-style option trading symbol without a live chain fetch.

`OptionChainService.get_option_chain()`'s actual instrument rows are
ground truth from the broker — they carry whatever symbol/token/lot-size
the broker itself assigned, and are always the right thing to trade
against. This builder exists for the cases where a caller wants to
*compute* a symbol without paying for a live chain fetch first (e.g. a
quick lookup, or a unit test that shouldn't need a fake broker) — prefer
the chain's real rows whenever one is already in hand.
"""

from __future__ import annotations

from datetime import date

from app.options.models import OptionType


class OptionSymbolBuilder:
    """Stateless builder for Angel One's index-option symbol convention.

    Angel One's documented scrip-master `symbol` format for index options
    is `{UNDERLYING}{DDMMMYYYY}{STRIKE}{CE|PE}`, e.g. `NIFTY28JUL202617500CE`
    for NIFTY, expiry 28-Jul-2026, strike 17500, a call. `STRIKE` is always
    a whole rupee integer for index options (no paise/decimal component in
    the symbol itself, unlike the scrip master's raw `strike` field, which
    is in paise — see `app.brokers.angel_one_instruments`).
    """

    def build(self, underlying: str, expiry: date, strike: float, option_type: OptionType) -> str:
        """Return the Angel-One-style trading symbol for these parameters.

        Raises:
            ValueError: `underlying` is blank, or `strike <= 0`.
        """

        if not underlying.strip():
            raise ValueError("underlying must not be blank")
        if strike <= 0:
            raise ValueError(f"strike must be positive, got {strike}")

        expiry_part = expiry.strftime("%d%b%Y").upper()
        strike_part = int(round(strike))
        return f"{underlying.strip().upper()}{expiry_part}{strike_part}{option_type.value}"
