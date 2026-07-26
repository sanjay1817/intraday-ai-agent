"""Unit tests for `UpstoxAdapter` (app.brokers.upstox).

Every test backs the adapter's HTTP client with `httpx.MockTransport` —
no real network call is ever made — and exercises behavior only through
the public `BrokerInterface` methods, so the shared retry / token-expiry /
WebSocket-reconnect plumbing in `BaseBrokerAdapter` / `ws_client` is
exercised exactly as production code would exercise it.
"""

import asyncio
import json
import urllib.parse
from collections.abc import Callable
from datetime import datetime

import httpx
import pytest

from app.brokers.upstox import UpstoxAdapter
from app.config.settings import BrokerSettings, UpstoxCredentials
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
        transport=httpx.MockTransport(handler), base_url="https://api.upstox.com/v2"
    )


def make_credentials(**overrides: str) -> UpstoxCredentials:
    """Default test credentials, overridable per test."""

    defaults = {
        "api_key": "key123",
        "api_secret": "secret456",
        "redirect_uri": "https://example.com/callback",
        "auth_code": "authcode789",
        "base_url": "https://api.upstox.com/v2",
    }
    defaults.update(overrides)
    return UpstoxCredentials(**defaults)


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


async def test_login_sends_form_encoded_body_and_parses_token_bundle() -> None:
    captured: dict[str, dict[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v2/login/authorization/token"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        captured["body"] = parsed_form_body(request)
        return httpx.Response(
            200,
            json={
                "access_token": "abc123",
                "user_id": "AB1234",
                "user_name": "Test User",
                "email": "test@example.com",
            },
        )

    creds = make_credentials()
    adapter = UpstoxAdapter(creds, make_fast_settings(), http_client=make_client(handler))

    bundle = await adapter.login()

    assert captured["body"]["code"] == creds.auth_code
    assert captured["body"]["client_id"] == creds.api_key
    assert captured["body"]["client_secret"] == creds.api_secret
    assert captured["body"]["redirect_uri"] == creds.redirect_uri
    assert captured["body"]["grant_type"] == "authorization_code"
    assert bundle.access_token == "abc123"
    assert bundle.refresh_token is None
    assert adapter._access_token == "abc123"


async def test_refresh_token_always_raises() -> None:
    adapter = UpstoxAdapter(
        make_credentials(),
        make_fast_settings(),
        http_client=make_client(lambda r: httpx.Response(200, json={})),
    )

    with pytest.raises(BrokerAuthenticationError):
        await adapter.refresh_token()


# -- profile ------------------------------------------------------------------------


async def test_get_profile_parses_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/user/profile"
        assert request.headers["Authorization"] == "Bearer tok"
        assert request.headers["Accept"] == "application/json"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "user_id": "AB1234",
                    "user_name": "Test User",
                    "email": "test@example.com",
                    "exchanges": ["NSE", "BSE", "SOMETHING_UNKNOWN"],
                    "products": ["D", "I", "MTF", "CO", "SOMETHING_UNKNOWN"],
                },
            },
        )

    adapter = UpstoxAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    profile = await adapter.get_profile()

    assert profile.broker == BrokerName.UPSTOX
    assert profile.client_id == "AB1234"
    assert profile.display_name == "Test User"
    assert profile.email == "test@example.com"
    assert set(profile.exchanges_enabled) == {Exchange.NSE, Exchange.BSE}
    assert set(profile.products_enabled) == {
        ProductType.DELIVERY,
        ProductType.INTRADAY,
        ProductType.MARGIN,
        ProductType.COVER_ORDER,
    }


# -- orders -------------------------------------------------------------------------


async def test_place_order_builds_request_and_parses_response() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path.removeprefix("/v2")
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200, json={"status": "success", "data": {"order_id": "230101000000001"}}
        )

    adapter = UpstoxAdapter(
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

    assert captured["path"] == "/order/place"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["instrument_token"] == "NSE|INFY"
    assert body["transaction_type"] == "BUY"
    assert body["order_type"] == "LIMIT"
    assert body["quantity"] == 10
    assert body["product"] == "I"
    assert body["price"] == 1500.5
    assert body["trigger_price"] == 0
    assert body["is_amo"] is False
    assert response.order_id == "230101000000001"
    assert response.broker == BrokerName.UPSTOX


async def test_place_order_rejection_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            400,
            json={
                "status": "error",
                "errors": [{"errorCode": "UDAPI1004", "message": "Insufficient funds"}],
            },
        )

    adapter = UpstoxAdapter(
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
                "status": "success",
                "data": {"equity": {"available_margin": 1000.0, "used_margin": 100.0}},
            },
        )

    adapter = UpstoxAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    funds = await adapter.get_funds()

    assert calls["n"] > 1
    assert funds.available_cash == 1000.0
    assert funds.total_balance == 1100.0


async def test_expired_token_triggers_refresh_then_retries_successfully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                401,
                json={
                    "status": "error",
                    "errors": [{"errorCode": "UDAPI100050", "message": "Invalid token"}],
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"equity": {"available_margin": 50.0, "used_margin": 5.0}},
            },
        )

    adapter = UpstoxAdapter(
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


async def test_expired_token_with_unparseable_body_is_still_treated_as_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, text="not json at all")
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"equity": {"available_margin": 1.0, "used_margin": 1.0}},
            },
        )

    adapter = UpstoxAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "expiring-token"

    async def fake_refresh_token() -> None:
        adapter._access_token = "refreshed-token"

    monkeypatch.setattr(adapter, "refresh_token", fake_refresh_token)

    funds = await adapter.get_funds()

    assert calls["n"] == 2
    assert funds.available_cash == 1.0


# -- market data ----------------------------------------------------------------------


async def test_ltp_parses_last_price() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/market-quote/ltp"
        assert request.url.params["instrument_key"] == "NSE|INFY"
        return httpx.Response(
            200,
            json={"status": "success", "data": {"NSE_EQ:INFY": {"last_price": 1550.25}}},
        )

    adapter = UpstoxAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    quote = await adapter.ltp(Exchange.NSE, "INFY")

    assert quote.tradingsymbol == "INFY"
    assert quote.exchange == Exchange.NSE
    assert quote.last_price == 1550.25


async def test_historical_data_uses_dated_endpoint_for_day_interval() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/historical-candle/NSE|INFY/day/2024-01-02/2024-01-01"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "candles": [
                        ["2024-01-01T09:15:00+05:30", 100.0, 105.0, 99.0, 104.0, 10000, 0],
                        ["2024-01-02T09:15:00+05:30", 104.0, 108.0, 103.0, 107.0, 12000, 0],
                    ]
                },
            },
        )

    adapter = UpstoxAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    bars = await adapter.historical_data(
        Exchange.NSE, "INFY", HistoricalInterval.ONE_DAY, datetime(2024, 1, 1), datetime(2024, 1, 2)
    )

    assert len(bars) == 2
    assert bars[0].open == 100.0
    assert bars[0].close == 104.0
    assert bars[0].volume == 10000
    assert bars[1].close == 107.0


async def test_historical_data_uses_intraday_endpoint_for_sub_day_interval() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/historical-candle/intraday/NSE|INFY/30minute"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "candles": [["2024-01-01T09:15:00+05:30", 100.0, 101.0, 99.5, 100.5, 500, 0]]
                },
            },
        )

    adapter = UpstoxAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    bars = await adapter.historical_data(
        Exchange.NSE,
        "INFY",
        HistoricalInterval.THIRTY_MINUTE,
        datetime(2024, 1, 1),
        datetime(2024, 1, 1),
    )

    assert len(bars) == 1
    assert bars[0].close == 100.5
    assert bars[0].volume == 500


# -- WebSocket streaming ----------------------------------------------------------------


class _FakeConnection:
    """Records sent frames and yields two fake frames before "disconnecting".

    The forced disconnect (an arbitrary exception from `__anext__` after the
    scripted frames) is what lets `ReconnectingWebSocketClient._run` reach its
    `_sleep_or_stop` suspension point, which is the only place it truly
    yields control back to the event loop — without it the reconnect loop
    would spin forever inside a single asyncio task and the test would hang.
    """

    def __init__(self, frames: list[str | bytes]) -> None:
        self.sent: list[str] = []
        self._frames = list(frames)

    async def send(self, message: str) -> None:
        self.sent.append(message)

    def __aiter__(self) -> "_FakeConnection":
        return self

    async def __anext__(self) -> str | bytes:
        if self._frames:
            return self._frames.pop(0)
        raise ConnectionResetError("simulated disconnect to end the test's single pass")


class _FakeConnectCM:
    """Fakes `websockets.asyncio.client.connect`'s async-context-manager return value."""

    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _FakeConnection:
        return self._connection

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


async def test_start_websocket_subscribes_and_parses_json_tick_but_ignores_binary_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    json_frame = json.dumps({"feeds": {"NSE|INFY": {"ltpc": {"ltp": 1550.25}}}})
    binary_frame = b"\xff\xfe\x00\x01not-valid-utf8-or-json\xff"
    connection = _FakeConnection([json_frame, binary_frame])

    monkeypatch.setattr("app.brokers.ws_client.connect", lambda url: _FakeConnectCM(connection))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/feed/market-data-feed/authorize"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"authorized_redirect_uri": "wss://feed.upstox.com/xyz"},
            },
        )

    received: list[Quote] = []

    async def on_tick(quote: Quote) -> None:
        received.append(quote)

    adapter = UpstoxAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    await adapter.start_websocket(on_tick, [(Exchange.NSE, "INFY")])
    try:
        for _ in range(50):
            await asyncio.sleep(0)
            if received:
                break
    finally:
        await adapter.stop_websocket()

    assert len(connection.sent) >= 1
    subscribe_message = json.loads(connection.sent[0])
    assert subscribe_message["method"] == "sub"
    assert subscribe_message["data"] == {"mode": "ltpc", "instrumentKeys": ["NSE|INFY"]}

    assert len(received) == 1
    assert received[0].tradingsymbol == "INFY"
    assert received[0].exchange == Exchange.NSE
    assert received[0].last_price == 1550.25
    assert adapter._warned_binary_frame is True
