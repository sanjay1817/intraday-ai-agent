"""Resolves an options underlying's own historical-data instrument.

`app.options.recommendation.generate_option_recommendation` needs to fetch
recent candles for the *underlying itself* (e.g. the NIFTY index's own
spot price), not for one of its option contracts — but Angel One's
historical-data API, like every other SmartAPI call, only understands
`(Exchange, tradingsymbol)` pairs exactly as Angel One's own scrip master
spells them, and that spelling is not the same as this codebase's
display name for the underlying (`"NIFTY"`, `"BANKNIFTY"`, `"FINNIFTY"`,
or an equity underlying's bare name).

This module is the seam that looks that pairing up from the broker's own
instrument master, the same way `AngelOneInstrumentMaster.resolve()`
already looks up `symboltoken`s instead of guessing them, and the same way
`app.options.option_chain_service.AngelOneOptionInstrumentSource` already
looks up *option contract* rows — this is the missing sibling lookup for
the underlying's own spot/index row, never a hardcoded symbol table.
"""

from __future__ import annotations

from typing import Any, Protocol

import structlog

from app.brokers.angel_one_instruments import AngelOneInstrumentMaster
from app.domain.enums.trading import Exchange
from app.options.exceptions import UnderlyingInstrumentNotFoundError

logger = structlog.get_logger(__name__)

#: Angel One's derivatives-segment `instrumenttype` values — a matching
#: NSE row of one of these types is an option/future contract, never the
#: underlying's own spot/index instrument. A structurally similar but
#: distinct filter from `option_chain_service._OPTION_INSTRUMENT_TYPES`
#: (that one is scoped to `OPTIDX`/`OPTSTK` on NFO only, for a different
#: purpose — finding option rows, not excluding them); declared
#: independently here rather than imported, since that name is private to
#: its module.
_DERIVATIVE_INSTRUMENT_TYPES = frozenset({"OPTIDX", "OPTSTK", "FUTIDX", "FUTSTK"})

#: Angel One's cash segment code for NSE — the underlying's own spot/index
#: row (unlike its option contracts, which live on `"NFO"`) is listed here.
_NSE_SEGMENT = "NSE"

#: Angel One's documented scrip-master convention (see
#: `app.brokers.angel_one_instruments`'s module docstring): every NSE cash
#: *equity* listing's `symbol` ends in this suffix. An index has no cash
#: listing at all, so excluding `-EQ` rows is how this module tells "the
#: underlying's own spot/index row" apart from "this name's equity
#: listing" for underlyings that are actual stocks, not indices.
_EQUITY_SYMBOL_SUFFIX = "-EQ"

#: Angel One's `instrumenttype` for its actual queryable index-quote
#: instrument (confirmed by fetching the live public scrip master:
#: NIFTY/BANKNIFTY/FINNIFTY each have exactly one `"AMXIDX"` row —
#: `token=99926000`/`"Nifty 50"`, `token=99926009`/`"Nifty Bank"`,
#: `token=99926037`/`"Nifty Fin Service"` respectively — which is the one
#: Angel One's `getCandleData` actually returns bars for). Every index
#: name in the scrip master ALSO has a second NSE row sharing the bare
#: display name as its `symbol` (e.g. `token=26000`/`"NIFTY"`) with an
#: EMPTY `instrumenttype` and a sentinel `strike` of `"-1.000000"` — a
#: legacy/placeholder entry, not a live-quotable instrument, that
#: `getCandleData` returns zero bars for. Both rows pass every other
#: filter in `_is_matching_row` (NSE, non-`-EQ`, non-derivative), so
#: without this preference the two are ambiguous and a naive tiebreak
#: (e.g. "shortest symbol") picks the placeholder every time, since
#: `"NIFTY"` is shorter than `"Nifty 50"` — this was the exact root cause
#: of every index historical-data request failing with
#: `NoHistoricalDataError` while equities worked fine. A category
#: constant, not a hardcoded symbol/token — the same kind of Angel One
#: `instrumenttype` vocabulary this codebase already hardcodes elsewhere
#: (see `option_chain_service._OPTION_INSTRUMENT_TYPES`).
_INDEX_QUOTE_INSTRUMENT_TYPE = "AMXIDX"


class UnderlyingResolver(Protocol):
    """Structural type for `generate_option_recommendation`'s injected
    resolver. Lets tests substitute a trivial fake without needing a real
    `AngelOneInstrumentMaster` and its disk cache/HTTP download — mirrors
    `OptionInstrumentSource` in `app.options.option_chain_service` and
    `SymbolResolver` in `app.brokers.angel_one_instruments`.
    """

    async def resolve_historical_instrument(self, underlying: str) -> tuple[Exchange, str]: ...


class AngelOneUnderlyingResolver:
    """Resolves an options underlying (NIFTY/BANKNIFTY/FINNIFTY/a stock
    underlying) to the `(Exchange, tradingsymbol)` pair Angel One's
    historical-data API actually understands for that instrument's own
    spot price — sourced from the scrip master, never a hardcoded symbol
    table (Angel One's exact index symbol strings are not something this
    codebase should assume/guess; the scrip master is the single source
    of truth for them, exactly like `AngelOneInstrumentMaster.resolve()`
    already treats `symboltoken` lookups).
    """

    def __init__(self, instrument_master: AngelOneInstrumentMaster) -> None:
        self._instrument_master = instrument_master
        #: Underlyings are a tiny, fixed, rarely-changing set, and this is
        #: a pure `(name) -> (exchange, symbol)` mapping — the SAME
        #: underlying instrument every time, unlike `OptionChainService`'s
        #: chain cache (which needs a TTL because which strikes/expiries
        #: are *currently listed* genuinely changes over time). No TTL
        #: invalidation is needed here: once a name resolves to a scrip-
        #: master row, that row identifies the same instrument for the
        #: rest of this process's lifetime.
        self._cache: dict[str, tuple[Exchange, str]] = {}

    async def resolve_historical_instrument(self, underlying: str) -> tuple[Exchange, str]:
        """Return the `(Exchange, tradingsymbol)` pair to fetch historical
        candles for `underlying`'s own spot/index price.

        Raises:
            UnderlyingInstrumentNotFoundError: no NSE row in the
                instrument master matches `underlying` once cash-equity
                (`-EQ`) and derivative (option/future) rows are excluded.
            BrokerConnectionError: propagated from
                `AngelOneInstrumentMaster.rows()` (network failure
                downloading the scrip master).
            BrokerAPIError: propagated from
                `AngelOneInstrumentMaster.rows()` (a non-2xx/malformed
                scrip-master response).
        """

        target = underlying.strip().upper()
        cached = self._cache.get(target)
        if cached is not None:
            return cached

        rows = await self._instrument_master.rows()
        matches = [row for row in rows if self._is_matching_row(row, target)]

        if not matches:
            raise UnderlyingInstrumentNotFoundError(
                f"no NSE index/equity instrument found for underlying {target!r} "
                "in the Angel One instrument master",
                underlying=target,
            )

        if len(matches) > 1:
            logger.debug(
                "angel_one_underlying_resolution_ambiguous",
                underlying=target,
                candidate_symbols=[str(row.get("symbol")) for row in matches],
            )

        # Prefer Angel One's real index-quote instrument when one is among
        # the candidates (see `_INDEX_QUOTE_INSTRUMENT_TYPE`'s docstring
        # for why this disambiguation is necessary and how it was
        # confirmed against live data) — this is the fix for every index
        # historical-data request otherwise resolving to an unquotable
        # placeholder row. Falls back to the shortest-symbol heuristic
        # only when no `AMXIDX` row is present, e.g. for a future
        # non-index underlying this resolver might see.
        index_matches = [
            row for row in matches if row.get("instrumenttype") == _INDEX_QUOTE_INSTRUMENT_TYPE
        ]
        candidates = index_matches or matches
        best = min(candidates, key=lambda row: len(str(row.get("symbol") or "")))

        resolved = (Exchange.NSE, str(best["symbol"]))
        self._cache[target] = resolved
        return resolved

    @staticmethod
    def _is_matching_row(row: dict[str, Any], target: str) -> bool:
        name = str(row.get("name") or "").strip().upper()
        exch_seg = row.get("exch_seg")
        instrumenttype = row.get("instrumenttype")
        symbol = str(row.get("symbol") or "")
        return (
            name == target
            and exch_seg == _NSE_SEGMENT
            and not symbol.upper().endswith(_EQUITY_SYMBOL_SUFFIX)
            and instrumenttype not in _DERIVATIVE_INSTRUMENT_TYPES
        )
