"""Unit tests for `AngelOneUnderlyingResolver` (app.options.underlying_resolver).

Uses a hand-written fake exposing `.rows()` rather than a real
`AngelOneInstrumentMaster`/HTTP mocking — mirrors
`tests/unit/options/test_option_chain_service.py`'s `FakeSource` convention:
the seam is already abstracted (here, `AngelOneUnderlyingResolver` only
calls `instrument_master.rows()`), so no real broker/HTTP plumbing is
needed to exercise the resolver's matching/caching behavior.
"""

from typing import Any

import pytest

from app.domain.enums.trading import Exchange
from app.options.exceptions import UnderlyingInstrumentNotFoundError
from app.options.underlying_resolver import AngelOneUnderlyingResolver


class FakeInstrumentMaster:
    """A minimal stand-in for `AngelOneInstrumentMaster` exposing only the
    `.rows()` method `AngelOneUnderlyingResolver` actually calls.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.rows_calls = 0

    async def rows(self) -> list[dict[str, Any]]:
        self.rows_calls += 1
        return self._rows


# Every row set below mirrors the ACTUAL shape confirmed by fetching Angel
# One's live public scrip master (see `underlying_resolver.py`'s
# `_INDEX_QUOTE_INSTRUMENT_TYPE` docstring): each index name has TWO NSE,
# non-`-EQ`, non-derivative rows, not one -- the real `"AMXIDX"` index-quote
# instrument (`token=99926000`/`"Nifty 50"` for NIFTY, etc.) AND a second,
# unquotable legacy/placeholder row sharing the bare display name as its
# own `symbol` (`token=26000`/`"NIFTY"`, empty `instrumenttype`, sentinel
# `strike="-1.000000"`). This ambiguity -- not merely "a decoy equity/option
# row" -- was the actual root cause of every index historical-data request
# resolving to the wrong, unquotable instrument; these fixtures reproduce it
# exactly rather than a simplified version that wouldn't have caught the bug.
_NIFTY_ROWS = [
    {
        "token": "99926000",
        "symbol": "Nifty 50",
        "name": "NIFTY",
        "exch_seg": "NSE",
        "instrumenttype": "AMXIDX",
        "strike": "0.000000",
    },
    {
        "token": "26000",
        "symbol": "NIFTY",
        "name": "NIFTY",
        "exch_seg": "NSE",
        "instrumenttype": "",
        "strike": "-1.000000",
    },
    # Decoys: a cash-equity listing and an option contract, same `name`.
    {"token": "999", "symbol": "NIFTY-EQ", "name": "NIFTY", "exch_seg": "NSE", "instrumenttype": ""},
    {
        "token": "111",
        "symbol": "NIFTY28AUG202524000CE",
        "name": "NIFTY",
        "exch_seg": "NFO",
        "instrumenttype": "OPTIDX",
    },
]

_BANKNIFTY_ROWS = [
    {
        "token": "99926009",
        "symbol": "Nifty Bank",
        "name": "BANKNIFTY",
        "exch_seg": "NSE",
        "instrumenttype": "AMXIDX",
        "strike": "0.000000",
    },
    {
        "token": "26009",
        "symbol": "BANKNIFTY",
        "name": "BANKNIFTY",
        "exch_seg": "NSE",
        "instrumenttype": "",
        "strike": "-1.000000",
    },
    {"token": "998", "symbol": "BANKNIFTY-EQ", "name": "BANKNIFTY", "exch_seg": "NSE", "instrumenttype": ""},
    {
        "token": "222",
        "symbol": "BANKNIFTY28AUG202548000PE",
        "name": "BANKNIFTY",
        "exch_seg": "NFO",
        "instrumenttype": "OPTIDX",
    },
]

_FINNIFTY_ROWS = [
    {
        "token": "99926037",
        "symbol": "Nifty Fin Service",
        "name": "FINNIFTY",
        "exch_seg": "NSE",
        "instrumenttype": "AMXIDX",
        "strike": "0.000000",
    },
    {
        "token": "26037",
        "symbol": "FINNIFTY",
        "name": "FINNIFTY",
        "exch_seg": "NSE",
        "instrumenttype": "",
        "strike": "-1.000000",
    },
    {"token": "997", "symbol": "FINNIFTY-EQ", "name": "FINNIFTY", "exch_seg": "NSE", "instrumenttype": ""},
    {
        "token": "333",
        "symbol": "FINNIFTY28AUG202521000CE",
        "name": "FINNIFTY",
        "exch_seg": "NFO",
        "instrumenttype": "OPTIDX",
    },
]


async def test_nifty_resolves_to_its_own_spot_row_ignoring_decoys() -> None:
    master = FakeInstrumentMaster(_NIFTY_ROWS)
    resolver = AngelOneUnderlyingResolver(master)

    exchange, tradingsymbol = await resolver.resolve_historical_instrument("NIFTY")

    assert exchange == Exchange.NSE
    assert tradingsymbol == "Nifty 50"


async def test_banknifty_resolves_to_its_own_spot_row() -> None:
    master = FakeInstrumentMaster(_BANKNIFTY_ROWS)
    resolver = AngelOneUnderlyingResolver(master)

    exchange, tradingsymbol = await resolver.resolve_historical_instrument("BANKNIFTY")

    assert exchange == Exchange.NSE
    assert tradingsymbol == "Nifty Bank"


async def test_finnifty_resolves_to_its_own_spot_row() -> None:
    master = FakeInstrumentMaster(_FINNIFTY_ROWS)
    resolver = AngelOneUnderlyingResolver(master)

    exchange, tradingsymbol = await resolver.resolve_historical_instrument("FINNIFTY")

    assert exchange == Exchange.NSE
    assert tradingsymbol == "Nifty Fin Service"


async def test_prefers_amxidx_index_row_over_shorter_placeholder_symbol() -> None:
    """Regression test for the actual production bug: `"NIFTY"` (the
    placeholder row's `symbol`, `instrumenttype=""`) is shorter than
    `"Nifty 50"` (the real `"AMXIDX"` index-quote row's `symbol`), so a
    naive shortest-symbol tiebreak picks the placeholder -- which Angel
    One's `getCandleData` returns zero bars for, surfacing as
    `NoHistoricalDataError` for every index despite `SBIN-EQ` working fine.
    The resolver must prefer the `AMXIDX` row whenever one is present among
    the candidates, regardless of symbol length.
    """

    master = FakeInstrumentMaster(_NIFTY_ROWS)
    resolver = AngelOneUnderlyingResolver(master)

    exchange, tradingsymbol = await resolver.resolve_historical_instrument("NIFTY")

    assert exchange == Exchange.NSE
    assert tradingsymbol == "Nifty 50"  # NOT the shorter placeholder "NIFTY"


async def test_no_matching_row_anywhere_raises_not_found() -> None:
    master = FakeInstrumentMaster([])
    resolver = AngelOneUnderlyingResolver(master)

    with pytest.raises(UnderlyingInstrumentNotFoundError):
        await resolver.resolve_historical_instrument("NIFTY")


async def test_only_equity_and_option_rows_raises_not_found() -> None:
    """No plain index/spot row exists for this name -- only an `-EQ` cash
    listing and option contracts -- proving the exclusion filters actually
    work, not just happen to pass because nothing else was there either.
    """

    rows = [
        {"token": "999", "symbol": "NIFTY-EQ", "name": "NIFTY", "exch_seg": "NSE", "instrumenttype": ""},
        {
            "token": "111",
            "symbol": "NIFTY28AUG202524000CE",
            "name": "NIFTY",
            "exch_seg": "NFO",
            "instrumenttype": "OPTIDX",
        },
    ]
    master = FakeInstrumentMaster(rows)
    resolver = AngelOneUnderlyingResolver(master)

    with pytest.raises(UnderlyingInstrumentNotFoundError):
        await resolver.resolve_historical_instrument("NIFTY")


async def test_resolution_is_cached_across_repeated_calls() -> None:
    master = FakeInstrumentMaster(_NIFTY_ROWS)
    resolver = AngelOneUnderlyingResolver(master)

    first = await resolver.resolve_historical_instrument("NIFTY")
    second = await resolver.resolve_historical_instrument("NIFTY")

    assert first == second == (Exchange.NSE, "Nifty 50")
    assert master.rows_calls == 1


async def test_two_different_underlyings_resolve_independently_without_cross_contamination() -> None:
    combined_rows = _NIFTY_ROWS + _BANKNIFTY_ROWS
    master = FakeInstrumentMaster(combined_rows)
    resolver = AngelOneUnderlyingResolver(master)

    nifty = await resolver.resolve_historical_instrument("NIFTY")
    banknifty = await resolver.resolve_historical_instrument("BANKNIFTY")

    assert nifty == (Exchange.NSE, "Nifty 50")
    assert banknifty == (Exchange.NSE, "Nifty Bank")
    # Each underlying's first resolution still consults `.rows()` once
    # (the cache is per-underlying, not a global "already fetched once"
    # short-circuit) -- two distinct underlyings means two calls.
    assert master.rows_calls == 2
