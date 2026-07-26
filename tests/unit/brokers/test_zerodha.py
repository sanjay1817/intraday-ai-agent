"""Unit tests for `ZerodhaAdapter` (app.brokers.zerodha).

Every test backs the adapter's HTTP client with `httpx.MockTransport` —
no real network call is ever made — and exercises behavior only through
the public `BrokerInterface` methods, so the shared retry / token-expiry /
WebSocket-reconnect plumbing in `BaseBrokerAdapter` / `ws_client` is
exercised exactly as production code would exercise it.
"""

import asyncio
import hashlib
import json
import struct
import urllib.parse
from collections.abc import Callable
from datetime import datetime

import httpx
import pytest

from app.brokers.zerodha import ZerodhaAdapter
from app.config.settings import BrokerSettings, ZerodhaCredentials
from app.domain.entities.broker import OrderRequest, Quote
from app.domain.enums.trading import (
    BrokerName,
    Exchange,
    HistoricalInterval,
    OrderSide,
    OrderType,
    ProductType,
)
from app.domain.exceptions.broker import BrokerAuthenticationError, OrderRejectionError

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler) -> httpx.AsyncClient:
    """Build an `httpx.AsyncClient` backed by a `MockTransport` (no real network)."""

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.kite.trade"
    )


def make_credentials(**overrides: str) -> ZerodhaCredentials:
    """Default test credentials, overridable per test."""

    defaults = {
        "api_key": "key123",
        "api_secret": "secret456",
        "request_token": "reqtok789",
        "base_url": "https://api.kite.trade",
    }
    defaults.update(overrides)
    return ZerodhaCredentials(**defaults)


def make_fast_settings() -> BrokerSettings:
    """Broker settings with tiny backoff so retry tests run fast."""

    return BrokerSettings(
        request_timeout_seconds=5.0,
        max_retries=2,
        retry_backoff_seconds=0.01,
        retry_max_backoff_seconds=0.02,
    )


def parsed_form_body(request: httpx.Request) -> dict[str, str]:
    """Decode a form-urlencoded request body into a flat dict."""

    pairs = urllib.parse.parse_qs(request.content.decode())
    return {key: values[0] for key, values in pairs.items()}


# -- login / refresh_token ----------------------------------------------------------


async def test_login_sends_checksum_and_parses_token_bundle() -> None:
    captured: dict[str, dict[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/session/token"
        captured["body"] = parsed_form_body(request)
        return httpx.Response(
            200,
            json={"data": {"access_token": "abc123", "refresh_token": None, "user_id": "AB1234"}},
        )

    creds = make_credentials()
    adapter = ZerodhaAdapter(creds, make_fast_settings(), http_client=make_client(handler))

    bundle = await adapter.login()

    expected_checksum = hashlib.sha256(
        f"{creds.api_key}{creds.request_token}{creds.api_secret}".encode()
    ).hexdigest()
    assert captured["body"]["api_key"] == creds.api_key
    assert captured["body"]["request_token"] == creds.request_token
    assert captured["body"]["checksum"] == expected_checksum
    assert bundle.access_token == "abc123"
    assert bundle.refresh_token is None
    assert adapter._access_token == "abc123"


async def test_refresh_token_raises_when_no_refresh_token_available() -> None:
    adapter = ZerodhaAdapter(
        make_credentials(),
        make_fast_settings(),
        http_client=make_client(lambda r: httpx.Response(200, json={})),
    )

    with pytest.raises(BrokerAuthenticationError):
        await adapter.refresh_token()


# -- profile ------------------------------------------------------------------------


async def test_get_profile_parses_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/user/profile"
        assert request.headers["Authorization"] == "token key123:tok"
        assert request.headers["X-Kite-Version"] == "3"
        return httpx.Response(
            200,
            json={
                "data": {
                    "user_id": "AB1234",
                    "user_name": "Test User",
                    "email": "test@example.com",
                    "exchanges": ["NSE", "BSE", "NFO", "SOMETHING_UNKNOWN"],
                    "products": ["CNC", "MIS", "NRML", "SOMETHING_UNKNOWN"],
                }
            },
        )

    adapter = ZerodhaAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    profile = await adapter.get_profile()

    assert profile.broker == BrokerName.ZERODHA
    assert profile.client_id == "AB1234"
    assert profile.display_name == "Test User"
    assert profile.email == "test@example.com"
    assert set(profile.exchanges_enabled) == {Exchange.NSE, Exchange.BSE, Exchange.NFO}
    assert set(profile.products_enabled) == {
        ProductType.DELIVERY,
        ProductType.INTRADAY,
        ProductType.MARGIN,
    }


# -- orders -------------------------------------------------------------------------


async def test_place_order_builds_request_and_parses_response() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = parsed_form_body(request)
        return httpx.Response(200, json={"data": {"order_id": "230101000000001"}})

    adapter = ZerodhaAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"
    order = OrderRequest(
        tradingsymbol="INFY",
        exchange=Exchange.NSE,
        transaction_type=OrderSide.BUY,
        quantity=10,
        order_type=OrderType.LIMIT,
        product=ProductType.INTRADAY,
        price=1500.5,
    )

    response = await adapter.place_order(order)

    assert captured["path"] == "/orders/regular"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["tradingsymbol"] == "INFY"
    assert body["exchange"] == "NSE"
    assert body["transaction_type"] == "BUY"
    assert body["order_type"] == "LIMIT"
    assert body["quantity"] == "10"
    assert body["product"] == "MIS"
    assert body["price"] == "1500.5"
    assert "trigger_price" not in body
    assert response.order_id == "230101000000001"
    assert response.broker == BrokerName.ZERODHA


async def test_place_order_rejection_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            400,
            json={
                "status": "error",
                "error_type": "InputException",
                "message": "Insufficient funds",
            },
        )

    adapter = ZerodhaAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"
    order = OrderRequest(
        tradingsymbol="INFY",
        exchange=Exchange.NSE,
        transaction_type=OrderSide.BUY,
        quantity=1,
        order_type=OrderType.MARKET,
        product=ProductType.INTRADAY,
    )

    with pytest.raises(OrderRejectionError) as exc_info:
        await adapter.place_order(order)

    assert calls["n"] == 1
    assert "Insufficient funds" in str(exc_info.value)


# -- retry / token-expiry behavior (BaseBrokerAdapter plumbing) ----------------------


async def test_transient_server_error_is_retried_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="internal error")
        return httpx.Response(
            200,
            json={
                "data": {
                    "equity": {
                        "available": {"cash": 1000.0},
                        "utilised": {"debits": 100.0},
                        "net": 900.0,
                    }
                }
            },
        )

    adapter = ZerodhaAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    funds = await adapter.get_funds()

    assert calls["n"] > 1
    assert funds.available_cash == 1000.0


async def test_expired_token_triggers_refresh_then_retries_successfully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                403, json={"error_type": "TokenException", "message": "Token expired"}
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "equity": {
                        "available": {"cash": 50.0},
                        "utilised": {"debits": 5.0},
                        "net": 45.0,
                    }
                }
            },
        )

    adapter = ZerodhaAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "expiring-token"

    refresh_calls = {"n": 0}

    async def fake_refresh_token() -> None:
        refresh_calls["n"] += 1
        adapter._access_token = "refreshed-token"

    monkeypatch.setattr(adapter, "refresh_token", fake_refresh_token)

    funds = await adapter.get_funds()

    assert refresh_calls["n"] == 1
    assert calls["n"] == 2
    assert funds.available_cash == 50.0
    assert adapter._access_token == "refreshed-token"


# -- market data ----------------------------------------------------------------------


async def test_ltp_parses_last_price() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/quote/ltp"
        assert request.url.params["i"] == "NSE:INFY"
        return httpx.Response(
            200, json={"data": {"NSE:INFY": {"instrument_token": 408065, "last_price": 1550.25}}}
        )

    adapter = ZerodhaAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    quote = await adapter.ltp(Exchange.NSE, "INFY")

    assert quote.tradingsymbol == "INFY"
    assert quote.exchange == Exchange.NSE
    assert quote.last_price == 1550.25


async def test_historical_data_parses_candles() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/instruments/historical/408065/day"
        assert request.url.params["from"] == "2024-01-01"
        assert request.url.params["to"] == "2024-01-02"
        return httpx.Response(
            200,
            json={
                "data": {
                    "candles": [
                        ["2024-01-01T09:15:00+0530", 100.0, 105.0, 99.0, 104.0, 10000],
                        ["2024-01-02T09:15:00+0530", 104.0, 108.0, 103.0, 107.0, 12000],
                    ]
                }
            },
        )

    adapter = ZerodhaAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    bars = await adapter.historical_data(
        Exchange.NSE,
        "408065",
        HistoricalInterval.ONE_DAY,
        datetime(2024, 1, 1),
        datetime(2024, 1, 2),
    )

    assert len(bars) == 2
    assert bars[0].open == 100.0
    assert bars[0].close == 104.0
    assert bars[0].volume == 10000
    assert bars[1].close == 107.0


# -- WebSocket streaming ----------------------------------------------------------------


class _FakeConnection:
    """Records sent frames and yields one fake tick before "disconnecting".

    The forced disconnect (an arbitrary exception from `__anext__` after the
    first frame) is what lets `ReconnectingWebSocketClient._run` reach its
    `_sleep_or_stop` suspension point, which is the only place it truly
    yields control back to the event loop — without it the reconnect loop
    would spin forever inside a single asyncio task and the test would hang.
    """

    def __init__(self, frame: bytes) -> None:
        self.sent: list[str] = []
        self._frame = frame
        self._served = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    def __aiter__(self) -> "_FakeConnection":
        return self

    async def __anext__(self) -> bytes:
        if not self._served:
            self._served = True
            return self._frame
        raise ConnectionResetError("simulated disconnect to end the test's single pass")


class _FakeConnectCM:
    """Fakes `websockets.asyncio.client.connect`'s async-context-manager return value."""

    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _FakeConnection:
        return self._connection

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


async def test_start_websocket_subscribes_and_parses_ltp_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrument_token = 408065
    price_paise = 155025  # 1550.25 rupees

    # Kite LTP binary frame: 2-byte packet count, then per packet a 2-byte
    # length followed by an 8-byte body (int32 token, int32 price in paise).
    frame = (
        struct.pack(">H", 1)
        + struct.pack(">H", 8)
        + struct.pack(">ii", instrument_token, price_paise)
    )
    connection = _FakeConnection(frame)

    monkeypatch.setattr("app.brokers.ws_client.connect", lambda url: _FakeConnectCM(connection))

    received: list[Quote] = []

    async def on_tick(quote: Quote) -> None:
        received.append(quote)

    adapter = ZerodhaAdapter(
        make_credentials(),
        make_fast_settings(),
        http_client=make_client(lambda r: httpx.Response(200, json={})),
    )
    adapter._access_token = "tok"

    await adapter.start_websocket(on_tick, [(Exchange.NSE, str(instrument_token))])
    try:
        for _ in range(50):
            await asyncio.sleep(0)
            if received:
                break
    finally:
        await adapter.stop_websocket()

    assert len(connection.sent) >= 2
    assert json.loads(connection.sent[0]) == {"a": "subscribe", "v": [str(instrument_token)]}
    assert json.loads(connection.sent[1]) == {"a": "mode", "v": ["ltp", [str(instrument_token)]]}

    assert len(received) == 1
    assert received[0].tradingsymbol == str(instrument_token)
    assert received[0].exchange == Exchange.NSE
    assert received[0].last_price == 1550.25
