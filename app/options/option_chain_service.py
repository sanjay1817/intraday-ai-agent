"""Fetches, caches, and serves option chains for configured underlyings.

`OptionChainService` is the centerpiece of this package: it owns a
per-underlying TTL cache in front of a broker-agnostic
`OptionInstrumentSource` (the same seam-via-`Protocol` pattern
`SymbolResolver` uses in `app.brokers.angel_one_instruments`), and
translates that source's broker exceptions into this package's own
`OptionChainFetchError` at the boundary — nothing downstream of this
service ever needs to know which broker (or which broker exception type)
produced a chain.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any, Protocol

import structlog
from fastapi import Request

from app.brokers.angel_one_instruments import AngelOneInstrumentMaster
from app.domain.enums.trading import Exchange
from app.domain.exceptions.broker import BrokerAPIError, BrokerConnectionError
from app.options.exceptions import (
    InvalidOptionChainDataError,
    OptionChainFetchError,
    UnsupportedUnderlyingError,
)
from app.options.models import OptionChain, OptionInstrument, OptionType

logger = structlog.get_logger(__name__)

#: Angel One scrip-master `instrumenttype` values that represent options
#: (index options and stock options respectively); futures/equities are
#: never returned by `AngelOneOptionInstrumentSource`.
_OPTION_INSTRUMENT_TYPES = frozenset({"OPTIDX", "OPTSTK"})

#: Angel One's derivatives segment code.
_NFO_SEGMENT = "NFO"

#: Angel One's documented scrip-master convention: strike is stored in
#: paise, not rupees (see `app.brokers.angel_one_instruments`'s module
#: docstring).
_PAISE_PER_RUPEE = 100.0


class OptionInstrumentSource(Protocol):
    """Structural type for `OptionChainService`'s injected data source.

    Lets tests substitute a hand-written fake without needing a real
    broker adapter or HTTP mocking — mirrors `SymbolResolver` in
    `app.brokers.angel_one_instruments`.
    """

    async def fetch_option_instruments(self, underlying: str) -> list[OptionInstrument]: ...


class AngelOneOptionInstrumentSource:
    """`OptionInstrumentSource` backed by an `AngelOneInstrumentMaster`.

    Reuses the instrument master's cached raw row list (`.rows()`) rather
    than owning a second HTTP client/cache — one scrip-master download
    serves both plain symbol resolution (`AngelOneAdapter`) and option
    chain construction.
    """

    def __init__(self, instrument_master: AngelOneInstrumentMaster) -> None:
        self._instrument_master = instrument_master

    async def fetch_option_instruments(self, underlying: str) -> list[OptionInstrument]:
        """Return every parseable option instrument for `underlying`.

        Raises:
            InvalidOptionChainDataError: zero option rows for `underlying`
                survived filtering/parsing (and the fetch itself did not
                raise) — most likely `underlying` isn't currently listed,
                or isn't actually an index/stock underlying.
            BrokerConnectionError: propagated from `AngelOneInstrumentMaster.rows()`
                (network failure downloading the scrip master).
            BrokerAPIError: propagated from `AngelOneInstrumentMaster.rows()`
                (a non-2xx/malformed scrip-master response).
        """

        rows = await self._instrument_master.rows()
        target = underlying.strip().upper()

        instruments: list[OptionInstrument] = []
        for row in rows:
            if not self._is_matching_option_row(row, target):
                continue
            instrument = self._parse_row(row, target)
            if instrument is not None:
                instruments.append(instrument)

        if not instruments:
            raise InvalidOptionChainDataError(
                f"no parseable option instruments found for underlying {target!r}",
                underlying=target,
            )
        return instruments

    @staticmethod
    def _is_matching_option_row(row: dict[str, Any], target: str) -> bool:
        name = str(row.get("name") or "").strip().upper()
        exch_seg = row.get("exch_seg")
        instrumenttype = row.get("instrumenttype")
        symbol = str(row.get("symbol") or "")
        return (
            name == target
            and exch_seg == _NFO_SEGMENT
            and instrumenttype in _OPTION_INSTRUMENT_TYPES
            and (symbol.endswith("CE") or symbol.endswith("PE"))
        )

    @staticmethod
    def _parse_row(row: dict[str, Any], target: str) -> OptionInstrument | None:
        try:
            expiry = datetime.strptime(str(row["expiry"]), "%d%b%Y").date()
            strike = float(row["strike"]) / _PAISE_PER_RUPEE
            symbol = str(row["symbol"])
            option_type = OptionType.CE if symbol.endswith("CE") else OptionType.PE
            token = str(row["token"]) if row.get("token") is not None else None
            lot_size = int(row["lotsize"]) if row.get("lotsize") is not None else None
        except (KeyError, ValueError, TypeError) as exc:
            logger.debug(
                "angel_one_option_row_skipped_unparseable",
                underlying=target,
                row=row,
                error=str(exc),
            )
            return None

        return OptionInstrument(
            underlying=target,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            tradingsymbol=symbol,
            exchange=Exchange.NFO,
            token=token,
            lot_size=lot_size,
        )


class OptionChainService:
    """Caches and serves `OptionChain`s for a fixed set of underlyings.

    One `asyncio.Lock` guards the whole cache rather than one per
    underlying: this codebase's configured `option_underlyings` list is
    tiny (a handful of indices), so the simplicity of a single lock
    outweighs the extra concurrency a per-underlying lock would buy —
    concurrent first-calls for *different* underlyings serialize through
    it, but each is a cheap in-memory filter over an already-downloaded/
    cached scrip master, not a fresh network round trip.
    """

    def __init__(
        self,
        *,
        underlyings: Sequence[str],
        refresh_seconds: float,
        source: OptionInstrumentSource,
    ) -> None:
        if not underlyings:
            raise ValueError("underlyings must not be empty")
        self._underlyings = tuple(u.strip().upper() for u in underlyings)
        self._refresh_seconds = refresh_seconds
        self._source = source
        self._cache: dict[str, tuple[OptionChain, float]] = {}
        self._lock = asyncio.Lock()

    async def get_option_chain(self, underlying: str, *, force_refresh: bool = False) -> OptionChain:
        """Return the (possibly cached) option chain for `underlying`.

        Raises:
            UnsupportedUnderlyingError: `underlying` is not in the
                configured underlyings set (case-insensitive match).
            OptionChainFetchError: the source's fetch raised a broker
                connection/API failure.
            InvalidOptionChainDataError: propagated from the source when
                the fetch succeeded but produced zero parseable instruments.
        """

        normalized = self._normalize(underlying)

        async with self._lock:
            cached = self._cache.get(normalized)
            if cached is not None and not force_refresh:
                chain, fetched_monotonic = cached
                age = time.monotonic() - fetched_monotonic
                if age < self._refresh_seconds:
                    return chain
            return await self._fetch_and_cache(normalized)

    async def get_available_expiries(self, underlying: str) -> tuple[date, ...]:
        """Return the sorted, deduplicated expiries available for `underlying`."""

        chain = await self.get_option_chain(underlying)
        return chain.expiries()

    async def get_available_strikes(self, underlying: str, expiry: date) -> tuple[float, ...]:
        """Return the sorted, deduplicated strikes available for `underlying`/`expiry`."""

        chain = await self.get_option_chain(underlying)
        return chain.strikes_for_expiry(expiry)

    async def refresh(self, underlying: str | None = None) -> None:
        """Force-refresh one underlying, or every configured underlying if `None`."""

        targets = [self._normalize(underlying)] if underlying is not None else list(self._underlyings)
        for target in targets:
            await self.get_option_chain(target, force_refresh=True)

    def _normalize(self, underlying: str) -> str:
        normalized = underlying.strip().upper()
        if normalized not in self._underlyings:
            raise UnsupportedUnderlyingError(
                f"underlying {underlying!r} is not configured (expected one of {self._underlyings})",
                underlying=normalized,
            )
        return normalized

    async def _fetch_and_cache(self, normalized: str) -> OptionChain:
        try:
            instruments = await self._source.fetch_option_instruments(normalized)
        except (BrokerConnectionError, BrokerAPIError) as exc:
            raise OptionChainFetchError(
                f"failed to fetch option chain for {normalized}: {exc}",
                underlying=normalized,
            ) from exc

        chain = OptionChain(
            underlying=normalized,
            fetched_at=datetime.now(UTC),
            instruments=tuple(instruments),
        )
        self._cache[normalized] = (chain, time.monotonic())
        logger.info(
            "option_chain_refreshed", underlying=normalized, instrument_count=len(instruments)
        )
        return chain


def get_option_chain_service(request: Request) -> OptionChainService:
    """The shared `OptionChainService` constructed once at application
    startup (see `app.main`'s lifespan), when the resolved broker is
    Angel One — `None` otherwise. Not wired to any router this phase;
    ready for a future phase's option-chain endpoints.
    """

    return request.app.state.option_chain_service  # type: ignore[no-any-return]
