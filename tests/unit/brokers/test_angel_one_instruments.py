"""Unit tests for `AngelOneInstrumentMaster` (app.brokers.angel_one_instruments).

Every test backs the HTTP client with `httpx.MockTransport` (no real
network call) and points `cache_path` at a `tmp_path`-scoped file so
tests never touch the real OS temp cache the production default uses.
"""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app.brokers.angel_one_instruments import AngelOneInstrumentMaster
from app.domain.enums.trading import Exchange
from app.domain.exceptions.broker import (
    BrokerAPIError,
    BrokerConnectionError,
    InstrumentNotFoundError,
)

Handler = Callable[[httpx.Request], httpx.Response]

_SAMPLE_ROWS = [
    {"token": "3045", "symbol": "SBIN-EQ", "name": "SBIN", "exch_seg": "NSE"},
    {"token": "500325", "symbol": "RELIANCE", "name": "RELIANCE", "exch_seg": "BSE"},
    {"token": "26009", "symbol": "BANKNIFTY24JULFUT", "name": "BANKNIFTY", "exch_seg": "NFO"},
    # A malformed row (missing token) must be skipped, not crash indexing.
    {"symbol": "BROKEN-EQ", "exch_seg": "NSE"},
]


def make_client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def make_master(
    handler: Handler, tmp_path: Path, *, ttl_seconds: float = 3600.0
) -> AngelOneInstrumentMaster:
    return AngelOneInstrumentMaster(
        http_client=make_client(handler),
        cache_path=tmp_path / "scrip_master.json",
        ttl_seconds=ttl_seconds,
    )


def ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_SAMPLE_ROWS)


# -- resolve() matching behavior --------------------------------------------------


async def test_resolve_exact_symbol_match(tmp_path: Path) -> None:
    master = make_master(ok_handler, tmp_path)

    token = await master.resolve(Exchange.NSE, "SBIN-EQ")

    assert token == "3045"


async def test_resolve_bare_symbol_appends_eq_suffix_on_nse(tmp_path: Path) -> None:
    master = make_master(ok_handler, tmp_path)

    token = await master.resolve(Exchange.NSE, "SBIN")

    assert token == "3045"


async def test_resolve_bare_symbol_on_bse_matches_without_eq_suffix(tmp_path: Path) -> None:
    """BSE's cash-equity `symbol` field has no `-EQ` suffix at all (unlike
    NSE), so an exact match must be tried, not just the `-EQ` fallback.
    """

    master = make_master(ok_handler, tmp_path)

    token = await master.resolve(Exchange.BSE, "RELIANCE")

    assert token == "500325"


async def test_resolve_is_case_insensitive(tmp_path: Path) -> None:
    master = make_master(ok_handler, tmp_path)

    token = await master.resolve(Exchange.NSE, "sbin")

    assert token == "3045"


async def test_resolve_exact_match_works_for_non_eq_suffix_exchanges(tmp_path: Path) -> None:
    master = make_master(ok_handler, tmp_path)

    token = await master.resolve(Exchange.NFO, "BANKNIFTY24JULFUT")

    assert token == "26009"


async def test_resolve_does_not_apply_eq_suffix_fallback_outside_nse_bse(tmp_path: Path) -> None:
    """A bare/wrong symbol on a non-NSE/BSE segment must not silently
    match some unrelated `-EQ` row — it should raise, not guess.
    """

    master = make_master(ok_handler, tmp_path)

    with pytest.raises(InstrumentNotFoundError):
        await master.resolve(Exchange.NFO, "BANKNIFTY")


async def test_resolve_raises_when_not_found(tmp_path: Path) -> None:
    master = make_master(ok_handler, tmp_path)

    with pytest.raises(InstrumentNotFoundError) as exc_info:
        await master.resolve(Exchange.NSE, "NOSUCHSYMBOL")

    assert "NOSUCHSYMBOL" in str(exc_info.value)


async def test_malformed_row_is_skipped_not_fatal(tmp_path: Path) -> None:
    master = make_master(ok_handler, tmp_path)

    with pytest.raises(InstrumentNotFoundError):
        await master.resolve(Exchange.NSE, "BROKEN-EQ")


# -- disk cache ---------------------------------------------------------------------


async def test_resolve_downloads_and_writes_cache_on_first_call(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_SAMPLE_ROWS)

    cache_path = tmp_path / "scrip_master.json"
    master = AngelOneInstrumentMaster(
        http_client=make_client(handler), cache_path=cache_path, ttl_seconds=3600.0
    )

    await master.resolve(Exchange.NSE, "SBIN-EQ")

    assert calls["n"] == 1
    assert cache_path.exists()


async def test_resolve_uses_fresh_disk_cache_without_network_call(tmp_path: Path) -> None:
    cache_path = tmp_path / "scrip_master.json"
    cache_path.write_text('[{"token": "3045", "symbol": "SBIN-EQ", "exch_seg": "NSE"}]')

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not hit the network when a fresh cache file exists")

    master = AngelOneInstrumentMaster(
        http_client=make_client(handler), cache_path=cache_path, ttl_seconds=3600.0
    )

    token = await master.resolve(Exchange.NSE, "SBIN-EQ")

    assert token == "3045"


async def test_resolve_redownloads_when_cache_is_expired(tmp_path: Path) -> None:
    import os
    import time

    cache_path = tmp_path / "scrip_master.json"
    cache_path.write_text('[{"token": "OLD", "symbol": "SBIN-EQ", "exch_seg": "NSE"}]')
    stale_mtime = time.time() - 10_000
    os.utime(cache_path, (stale_mtime, stale_mtime))

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_SAMPLE_ROWS)

    master = AngelOneInstrumentMaster(
        http_client=make_client(handler), cache_path=cache_path, ttl_seconds=1.0
    )

    token = await master.resolve(Exchange.NSE, "SBIN-EQ")

    assert calls["n"] == 1
    assert token == "3045"  # the freshly downloaded value, not the stale "OLD"


async def test_resolve_only_downloads_once_across_concurrent_calls(tmp_path: Path) -> None:
    import asyncio

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_SAMPLE_ROWS)

    master = make_master(handler, tmp_path)

    results = await asyncio.gather(
        master.resolve(Exchange.NSE, "SBIN-EQ"),
        master.resolve(Exchange.NSE, "SBIN-EQ"),
        master.resolve(Exchange.BSE, "RELIANCE"),
    )

    assert calls["n"] == 1
    assert results == ["3045", "3045", "500325"]


# -- download failure paths ----------------------------------------------------------


async def test_download_failure_status_raises_broker_api_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    master = make_master(handler, tmp_path)

    with pytest.raises(BrokerAPIError):
        await master.resolve(Exchange.NSE, "SBIN-EQ")


async def test_download_network_failure_raises_broker_connection_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    master = make_master(handler, tmp_path)

    with pytest.raises(BrokerConnectionError):
        await master.resolve(Exchange.NSE, "SBIN-EQ")


async def test_download_non_list_body_raises_broker_api_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"})

    master = make_master(handler, tmp_path)

    with pytest.raises(BrokerAPIError):
        await master.resolve(Exchange.NSE, "SBIN-EQ")


# -- client lifecycle -----------------------------------------------------------------


async def test_close_closes_owned_client(tmp_path: Path) -> None:
    master = AngelOneInstrumentMaster(cache_path=tmp_path / "scrip_master.json")

    await master.close()

    assert master._client.is_closed


async def test_close_does_not_close_injected_client(tmp_path: Path) -> None:
    client = make_client(ok_handler)
    master = AngelOneInstrumentMaster(http_client=client, cache_path=tmp_path / "scrip_master.json")

    await master.close()

    assert not client.is_closed
    await client.aclose()


# -- rows() ---------------------------------------------------------------------------


async def test_rows_returns_raw_downloaded_rows(tmp_path: Path) -> None:
    master = make_master(ok_handler, tmp_path)

    rows = await master.rows()

    assert rows == _SAMPLE_ROWS


async def test_rows_and_resolve_share_a_single_download(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_SAMPLE_ROWS)

    master = make_master(handler, tmp_path)

    await master.rows()
    await master.resolve(Exchange.NSE, "SBIN-EQ")

    assert calls["n"] == 1
