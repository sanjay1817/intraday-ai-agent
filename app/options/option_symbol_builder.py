"""Builds an Angel-One-style option trading symbol without a live chain fetch.

`OptionChainService.get_option_chain()`'s actual instrument rows are
ground truth from the broker — they carry whatever symbol/token/lot-size
the broker itself assigned, and are always the right thing to trade
against; `app.options.recommendation.generate_option_recommendation` uses
`OptionChain.instrument_for(...)`'s real `tradingsymbol` rather than this
builder for exactly that reason. This builder exists for the cases where a
caller wants to *compute* a symbol without paying for a live chain fetch
first (e.g. a quick lookup, or a unit test that shouldn't need a fake
broker) — prefer the chain's real rows whenever one is already in hand.
"""

from __future__ import annotations

from datetime import date

from app.options.models import OptionType


class OptionSymbolBuilder:
    """Stateless builder for Angel One's index-option symbol convention.

    Angel One's actual scrip-master `symbol` format for index options is
    `{UNDERLYING}{DDMMMYY}{STRIKE}{CE|PE}` — a 2-digit year, e.g.
    `NIFTY28JUL2617500CE` for NIFTY, expiry 28-Jul-2026, strike 17500, a
    call — confirmed by fetching Angel One's live public scrip master
    (`https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json`)
    and inspecting real rows, e.g. `token=40885`/`symbol="NIFTY11AUG2621650CE"`
    for expiry `"11AUG2026"`/strike `2165000` (paise) = 21650 (rupees). An
    earlier version of this builder used a 4-digit year (`%Y` instead of
    `%y`), which produced a symbol Angel One's instrument master has no
    row for — every premium lookup built from that symbol failed with
    `InstrumentNotFoundError`/`PremiumUnavailableError` even when the
    correct instrument was already sitting in a fetched `OptionChain`. The
    scrip master's own `expiry` field is still 4-digit (`"11AUG2026"`);
    only the `symbol` string's embedded year is 2-digit — the two are not
    the same representation. `STRIKE` is always a whole rupee integer for
    index options (no paise/decimal component in the symbol itself, unlike
    the scrip master's raw `strike` field, which is in paise — see
    `app.brokers.angel_one_instruments`).
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

        expiry_part = expiry.strftime("%d%b%y").upper()
        strike_part = int(round(strike))
        return f"{underlying.strip().upper()}{expiry_part}{strike_part}{option_type.value}"
