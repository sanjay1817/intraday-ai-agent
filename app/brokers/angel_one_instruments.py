"""Angel One instrument-master symbol resolution.

SmartAPI's REST and WebSocket APIs identify every instrument by a numeric
`symboltoken` assigned in Angel One's own instrument-master dump, never
by a plain trading symbol — `AngelOneAdapter` cannot complete a single
LTP/historical/order/WebSocket-subscribe call against real data without
resolving one first. This module downloads that dump (published at a
fixed, unauthenticated public URL, refreshed by Angel One roughly daily;
~170k rows as of this writing), builds an in-memory
`(exchange, symbol) -> token` index from it, and caches the raw dump to
disk so a process restart within `ttl_seconds` reuses the last download
instead of re-fetching tens of megabytes.

Callers pass a trading symbol in the same shape Angel One's own `symbol`
field uses for cash equities (e.g. `"SBIN-EQ"`), or the bare underlying
name (e.g. `"SBIN"`) — the latter resolves via an automatic `"-EQ"`
suffix fallback on NSE/BSE, since that's the form most callers actually
have on hand and matching it exactly would otherwise fail every bare
lookup.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from tempfile import gettempdir
from typing import Any, Protocol

import httpx
import structlog

from app.domain.enums.trading import BrokerName, Exchange
from app.domain.exceptions.broker import (
    BrokerAPIError,
    BrokerConnectionError,
    InstrumentNotFoundError,
)

logger = structlog.get_logger(__name__)

#: Angel One's public, unauthenticated instrument-master dump. Referenced
#: from https://smartapi.angelbroking.com/docs/Instruments.
SCRIP_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"

#: A fixed path under the OS temp directory rather than inside the repo:
#: this is a re-derivable download cache, not application state, so it
#: doesn't belong in the working tree or need a `.gitignore` entry.
_DEFAULT_CACHE_PATH = Path(gettempdir()) / "intraday_agent_angel_one_scrip_master.json"

#: Angel One republishes this dump roughly daily; a day-long TTL avoids a
#: multi-ten-megabyte re-download on every process restart while still
#: picking up newly listed instruments within a day.
_DEFAULT_TTL_SECONDS = 24 * 60 * 60.0

#: The scrip master is tens of megabytes; generous relative to the other
#: broker HTTP calls in this codebase (`BrokerSettings.request_timeout_seconds`
#: defaults to 10s), which is appropriate for small trading API calls but
#: not for this bulk download.
_DOWNLOAD_TIMEOUT_SECONDS = 60.0

#: Exchanges whose cash-equity `symbol` field is always `"{name}-EQ"` —
#: a bare symbol with no explicit suffix is retried with `-EQ` appended
#: on these exchanges only.
_EQ_SUFFIX_EXCHANGES = frozenset({Exchange.NSE, Exchange.BSE})


class SymbolResolver(Protocol):
    """Structural type for `AngelOneAdapter`'s injected resolver.

    Lets tests substitute a trivial fake (returning some deterministic
    fixed token) without needing to build or mock a real
    `AngelOneInstrumentMaster` and its disk cache/HTTP download.
    """

    async def resolve(self, exchange: Exchange, tradingsymbol: str) -> str: ...


class AngelOneInstrumentMaster:
    """Resolves `(Exchange, tradingsymbol)` to Angel One's numeric `symboltoken`.

    Lazily downloads and indexes the instrument master on first
    `resolve()` call, not at construction, so building an
    `AngelOneAdapter` never does I/O by itself. The index then stays in
    memory for this instance's lifetime — concurrent first calls are
    coalesced onto a single download via an internal lock.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        cache_path: Path = _DEFAULT_CACHE_PATH,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._client = http_client or httpx.AsyncClient()
        #: Only close a client this instance created itself — an injected
        #: client's lifecycle belongs to whoever constructed it.
        self._owns_client = http_client is None
        self._cache_path = cache_path
        self._ttl_seconds = ttl_seconds
        self._index: dict[tuple[str, str], str] | None = None
        self._rows: list[dict[str, Any]] | None = None
        self._load_lock = asyncio.Lock()

    async def resolve(self, exchange: Exchange, tradingsymbol: str) -> str:
        """Return the numeric `symboltoken` for `tradingsymbol` on `exchange`.

        Raises:
            InstrumentNotFoundError: no matching row in the instrument master.
            BrokerConnectionError: the instrument master couldn't be
                downloaded (network failure) and no usable cache exists.
            BrokerAPIError: the instrument master download itself failed
                (non-2xx response or an unexpected body shape).
        """

        index = await self._get_index()
        symbol = tradingsymbol.strip().upper()

        token = index.get((exchange.value, symbol))
        if token is None and exchange in _EQ_SUFFIX_EXCHANGES and not symbol.endswith("-EQ"):
            token = index.get((exchange.value, f"{symbol}-EQ"))
        if token is None:
            raise InstrumentNotFoundError(
                f"angel_one: no instrument found for {exchange.value}:{tradingsymbol}",
                broker=BrokerName.ANGEL_ONE,
            )
        return token

    async def rows(self) -> list[dict[str, Any]]:
        """Return the raw downloaded/cached instrument-master row list.

        Additive to the `(exchange, symbol) -> token` index `resolve()`
        uses: callers that need fields the index doesn't carry (e.g.
        `expiry`/`strike`/`instrumenttype` for options — see
        `app.options.option_chain_service.AngelOneOptionInstrumentSource`)
        get them from here instead of a second, independent download.
        Shares this instance's cache-read/download/lock logic with
        `resolve()`'s index build, so the scrip master is fetched at most
        once per TTL window regardless of which caller asks first.

        Raises the same exceptions as `resolve()`'s underlying fetch:

        Raises:
            BrokerConnectionError: the instrument master couldn't be
                downloaded (network failure) and no usable cache exists.
            BrokerAPIError: the instrument master download itself failed
                (non-2xx response or an unexpected body shape).
        """

        return await self._get_rows()

    async def close(self) -> None:
        """Close the owned HTTP client, if this instance created one itself."""

        if self._owns_client:
            await self._client.aclose()

    async def _get_index(self) -> dict[tuple[str, str], str]:
        if self._index is not None:
            return self._index
        async with self._load_lock:
            if self._index is not None:
                return self._index
            raw = await self._get_rows_locked()
            self._index = self._build_index(raw)
            logger.info("angel_one_instrument_master_indexed", instrument_count=len(self._index))
            return self._index

    async def _get_rows(self) -> list[dict[str, Any]]:
        if self._rows is not None:
            return self._rows
        async with self._load_lock:
            return await self._get_rows_locked()

    async def _get_rows_locked(self) -> list[dict[str, Any]]:
        """Return the cached/downloaded raw rows. Caller must hold `_load_lock`."""

        if self._rows is not None:
            return self._rows
        raw = self._read_cache()
        if raw is None:
            raw = await self._download()
            self._write_cache(raw)
        self._rows = raw
        return self._rows

    def _read_cache(self) -> list[dict[str, Any]] | None:
        if not self._cache_path.exists():
            return None
        age_seconds = time.time() - self._cache_path.stat().st_mtime
        if age_seconds > self._ttl_seconds:
            return None
        try:
            with self._cache_path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, list) else None

    def _write_cache(self, data: list[dict[str, Any]]) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self._cache_path.open("w", encoding="utf-8") as f:
                json.dump(data, f)
        except OSError:
            logger.warning("angel_one_instrument_cache_write_failed", path=str(self._cache_path))

    async def _download(self) -> list[dict[str, Any]]:
        logger.info("angel_one_instrument_master_downloading", url=SCRIP_MASTER_URL)
        try:
            response = await self._client.get(SCRIP_MASTER_URL, timeout=_DOWNLOAD_TIMEOUT_SECONDS)
        except httpx.TransportError as exc:
            raise BrokerConnectionError(
                f"angel_one: failed to download instrument master: {exc}",
                broker=BrokerName.ANGEL_ONE,
            ) from exc

        if response.status_code >= 400:
            raise BrokerAPIError(
                f"angel_one: instrument master download failed with {response.status_code}",
                broker=BrokerName.ANGEL_ONE,
                status_code=response.status_code,
            )

        data = response.json()
        if not isinstance(data, list):
            raise BrokerAPIError(
                "angel_one: expected a JSON array from the instrument master, "
                f"got {type(data).__name__}",
                broker=BrokerName.ANGEL_ONE,
            )
        return data

    @staticmethod
    def _build_index(raw: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
        """Index every row by `(exch_seg, symbol)`, uppercased.

        Every segment (NSE/BSE cash, NFO/BFO derivatives, MCX, CDS, ...)
        is indexed, not just cash equities — `resolve()`'s `-EQ` fallback
        is the only segment-specific behavior; an exact `symbol` match
        (e.g. a full option/future symbol) works for any segment.
        """

        index: dict[tuple[str, str], str] = {}
        for entry in raw:
            exch_seg = entry.get("exch_seg")
            symbol = entry.get("symbol")
            token = entry.get("token")
            if not exch_seg or not symbol or not token:
                continue
            index[(exch_seg, str(symbol).upper())] = str(token)
        return index
