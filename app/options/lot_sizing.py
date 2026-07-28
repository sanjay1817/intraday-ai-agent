"""Resolves the lot size for one option contract.

An `OptionInstrument`'s `lot_size` — when present — comes straight from
the broker's own instrument master (Angel One's scrip-master `lotsize`
field; see `app.options.option_chain_service.AngelOneOptionInstrumentSource
._parse_row`), which is ground truth: it is exactly the contract
multiplier the broker itself will use to size a real order. `Settings
.option_default_lot_size` is only a documented fallback for the cases
where that field isn't available on the resolved instrument (or the
instrument can't be found in the chain at all) — never a value this
module prefers over the chain's own.
"""

from __future__ import annotations

from datetime import date

from app.options.models import OptionChain, OptionType


def resolve_lot_size(
    chain: OptionChain,
    *,
    expiry: date,
    strike: float,
    option_type: OptionType,
    default_lot_size: int,
) -> int:
    """Return the lot size for the contract `expiry`/`strike`/`option_type`
    within `chain`.

    Looks up `chain.instrument_for(...)`; if found and its `lot_size` is
    set, returns that broker-sourced value. Otherwise (no matching
    instrument, or one found with `lot_size is None`) falls back to
    `default_lot_size`.
    """

    instrument = chain.instrument_for(expiry, strike, option_type)
    if instrument is not None and instrument.lot_size is not None:
        return instrument.lot_size
    return default_lot_size
