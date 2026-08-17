"""Unit tests for `AngelOneAdapter` (app.brokers.angel_one).

Every test backs the adapter's HTTP client with `httpx.MockTransport` —
no real network call is ever made — and exercises behavior only through
the public `BrokerInterface` methods, so the shared retry / token-expiry /
WebSocket-reconnect plumbing in `BaseBrokerAdapter` / `ws_client` is
exercised exactly as production code would exercise it.
"""

import asyncio
import json
import struct
from collections.abc import Callable
from datetime import datetime

import httpx
import pyotp
import pytest

from app.brokers.angel_one import AngelOneAdapter
from app.config.settings import AngelOneCredentials, BrokerSettings
from app.domain.entities.broker import OrderRequest, Quote
from app.domain.enums.trading import (
    BrokerName,
    Exchange,
    HistoricalInterval,
    OrderSide,
    OrderStatus,
    OrderType,
    OrderValidity,
    ProductType,
)
from app.domain.exceptions.broker import (
    BrokerAPIError,
    BrokerAuthenticationError,
    OrderRejectionError,
)

Handler = Callable[[httpx.Request], httpx.Response]

_TOTP_SECRET = "JBSWY3DPEHPK3PXP"


class _FakeResolver:
    """Deterministic, network-free stand-in for `AngelOneInstrumentMaster`.

    Every adapter method that needs a `symboltoken` goes through
    `AngelOneAdapter._resolve_token`, which defaults to a real
    `AngelOneInstrumentMaster` (disk cache + a live HTTP download on first
    use) — every test that exercises one of those methods must inject
    this instead, or it would attempt a real network call.
    """

    def __init__(self, tokens: dict[tuple[Exchange, str], str] | None = None) -> None:
        self._tokens = tokens or {}

    async def resolve(self, exchange: Exchange, tradingsymbol: str) -> str:
        return self._tokens.get((exchange, tradingsymbol), f"TOKEN-{tradingsymbol}")


def make_client(handler: Handler) -> httpx.AsyncClient:
    """Build an `httpx.AsyncClient` backed by a `MockTransport` (no real network)."""

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://apiconnect.angelbroking.com"
    )


def make_tick_frame(
    token: str,
    *,
    exchange_type: int = 1,
    mode: int = 1,
    sequence: int = 1,
    timestamp_ms: int = 1_700_000_000_000,
    ltp_paise: int = 100000,
) -> bytes:
    """Build a SmartAPI LTP-mode (mode 1) binary tick packet: 51 bytes —
    mode(1) + exchange type(1) + 25-byte token + int64 LE sequence +
    int64 LE timestamp(ms) + int64 LE ltp(paise). See
    `AngelOneAdapter._TICK_TOKEN_LENGTH`'s comment for why 25 (not 24)
    bytes is the confirmed-correct token width.
    """

    return (
        struct.pack("<b", mode)
        + struct.pack("<b", exchange_type)
        + token.encode("ascii").ljust(25, b"\x00")
        + struct.pack("<q", sequence)
        + struct.pack("<q", timestamp_ms)
        + struct.pack("<q", ltp_paise)
    )


def make_credentials(**overrides: str) -> AngelOneCredentials:
    """Default test credentials, overridable per test."""

    defaults = {
        "client_code": "AB1234",
        "password": "secret-pass",
        "totp_secret": _TOTP_SECRET,
        "api_key": "apikey123",
        "base_url": "https://apiconnect.angelbroking.com",
    }
    defaults.update(overrides)
    return AngelOneCredentials(**defaults)


def make_fast_settings() -> BrokerSettings:
    """Broker settings with tiny backoff so retry tests run fast."""

    return BrokerSettings(
        request_timeout_seconds=5.0,
        max_retries=2,
        retry_backoff_seconds=0.01,
        retry_max_backoff_seconds=0.02,
    )


# -- login / refresh_token ----------------------------------------------------------


async def test_login_generates_totp_and_parses_token_bundle() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/rest/auth/angelbroking/user/v1/loginByPassword"
        assert request.headers["X-PrivateKey"] == "apikey123"
        assert request.headers["X-UserType"] == "USER"
        assert request.headers["X-SourceID"] == "WEB"
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "status": True,
                "message": "SUCCESS",
                "data": {
                    "jwtToken": "jwt-abc",
                    "refreshToken": "refresh-abc",
                    "feedToken": "feed-abc",
                },
            },
        )

    creds = make_credentials()
    adapter = AngelOneAdapter(creds, make_fast_settings(), http_client=make_client(handler))

    bundle = await adapter.login()

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["clientcode"] == creds.client_code
    assert body["password"] == creds.password
    expected_totp = pyotp.TOTP(_TOTP_SECRET).now()
    assert body["totp"] == expected_totp

    assert bundle.access_token == "jwt-abc"
    assert bundle.refresh_token == "refresh-abc"
    assert bundle.feed_token == "feed-abc"
    assert adapter._access_token == "jwt-abc"
    assert adapter._refresh_token_value == "refresh-abc"
    assert adapter._feed_token == "feed-abc"


async def test_login_raises_authentication_error_on_status_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # A non-"AG8*" errorcode: SmartAPI reserves the AG8xxx family for
        # expired/invalid *session* tokens on already-authenticated calls,
        # not for bad-credential login rejections, so this must NOT be
        # mistaken for a token-expiry signal by `_is_token_expired`.
        return httpx.Response(
            200, json={"status": False, "message": "Invalid totp", "errorcode": "AB1050"}
        )

    adapter = AngelOneAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )

    with pytest.raises(BrokerAuthenticationError) as exc_info:
        await adapter.login()

    assert "Invalid totp" in str(exc_info.value)


async def test_refresh_token_raises_when_never_logged_in() -> None:
    adapter = AngelOneAdapter(
        make_credentials(),
        make_fast_settings(),
        http_client=make_client(lambda r: httpx.Response(200, json={})),
    )

    with pytest.raises(BrokerAuthenticationError):
        await adapter.refresh_token()


# -- profile ------------------------------------------------------------------------


async def test_get_profile_parses_response() -> None:
    """Uses SmartAPI's real, lowercase, cash/derivatives-distinct segment
    codes and product codes (confirmed against a live account — see
    `_EXCHANGE_FROM_ANGEL_ONE_PROFILE`/`_PRODUCT_FROM_ANGEL_ONE`), not the
    plain `"NSE"`/`"BSE"` vocabulary the rest of this codebase uses.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/secure/angelbroking/user/v1/getProfile"
        assert request.headers["Authorization"] == "Bearer tok"
        assert request.headers["X-PrivateKey"] == "apikey123"
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {
                    "clientcode": "AB1234",
                    "name": "Test User",
                    "email": "test@example.com",
                    "exchanges": [
                        "nse_cm",
                        "nse_fo",
                        "bse_cm",
                        "bse_fo",
                        "cde_fo",
                        "mcx_fo",
                        "ncx_fo",
                    ],
                    "products": ["CNC", "MIS", "NRML", "SOMETHING_UNKNOWN"],
                },
            },
        )

    adapter = AngelOneAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    profile = await adapter.get_profile()

    assert profile.broker == BrokerName.ANGEL_ONE
    assert profile.client_id == "AB1234"
    assert profile.display_name == "Test User"
    assert profile.email == "test@example.com"
    # "ncx_fo" (NCDEX) has no `Exchange` member and must be skipped, not raise.
    assert set(profile.exchanges_enabled) == {
        Exchange.NSE,
        Exchange.NFO,
        Exchange.BSE,
        Exchange.BFO,
        Exchange.CDS,
        Exchange.MCX,
    }
    assert set(profile.products_enabled) == {
        ProductType.DELIVERY,  # from "CNC"
        ProductType.INTRADAY,
        ProductType.MARGIN,
    }


# -- orders -------------------------------------------------------------------------


async def test_place_order_builds_request_and_parses_response() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"status": True, "data": {"orderid": "230101000000001"}})

    adapter = AngelOneAdapter(
        make_credentials(),
        make_fast_settings(),
        http_client=make_client(handler),
        instrument_resolver=_FakeResolver({(Exchange.NSE, "INFY-EQ"): "1594"}),
    )
    adapter._access_token = "tok"
    order = OrderRequest(
        tradingsymbol="INFY-EQ",
        exchange=Exchange.NSE,
        transaction_type=OrderSide.BUY,
        quantity=10,
        order_type=OrderType.LIMIT,
        product=ProductType.INTRADAY,
        price=1500.5,
    )

    response = await adapter.place_order(order)

    assert captured["path"] == "/rest/secure/angelbroking/order/v1/placeOrder"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["tradingsymbol"] == "INFY-EQ"
    assert body["symboltoken"] == "1594"
    assert body["exchange"] == "NSE"
    assert body["transactiontype"] == "BUY"
    assert body["ordertype"] == "LIMIT"
    assert body["producttype"] == "INTRADAY"
    assert body["variety"] == "NORMAL"
    assert body["duration"] == "DAY"
    assert body["quantity"] == "10"
    assert body["price"] == "1500.5"
    assert response.order_id == "230101000000001"
    assert response.broker == BrokerName.ANGEL_ONE


async def test_buy_then_sell_stock_places_both_orders_correctly() -> None:
    """End-to-end-ish unit test: buy a stock, then sell it, via the Angel
    One adapter's `place_order`, asserting each request is built with the
    correct `transactiontype` and each response is parsed correctly.
    """

    captured_requests: list[dict[str, object]] = []
    order_ids = iter(["230101000000001", "230101000000002"])

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        captured_requests.append(body)
        return httpx.Response(
            200, json={"status": True, "data": {"orderid": next(order_ids)}}
        )

    adapter = AngelOneAdapter(
        make_credentials(),
        make_fast_settings(),
        http_client=make_client(handler),
        instrument_resolver=_FakeResolver({(Exchange.NSE, "INFY-EQ"): "1594"}),
    )
    adapter._access_token = "tok"

    buy_order = OrderRequest(
        tradingsymbol="INFY-EQ",
        exchange=Exchange.NSE,
        transaction_type=OrderSide.BUY,
        quantity=10,
        order_type=OrderType.MARKET,
        product=ProductType.DELIVERY,
    )
    buy_response = await adapter.place_order(buy_order)

    sell_order = OrderRequest(
        tradingsymbol="INFY-EQ",
        exchange=Exchange.NSE,
        transaction_type=OrderSide.SELL,
        quantity=10,
        order_type=OrderType.MARKET,
        product=ProductType.DELIVERY,
    )
    sell_response = await adapter.place_order(sell_order)

    assert len(captured_requests) == 2
    buy_body, sell_body = captured_requests

    assert buy_body["transactiontype"] == "BUY"
    assert buy_body["tradingsymbol"] == "INFY-EQ"
    assert buy_body["symboltoken"] == "1594"
    assert buy_body["quantity"] == "10"
    assert buy_response.order_id == "230101000000001"
    assert buy_response.broker == BrokerName.ANGEL_ONE

    assert sell_body["transactiontype"] == "SELL"
    assert sell_body["tradingsymbol"] == "INFY-EQ"
    assert sell_body["symboltoken"] == "1594"
    assert sell_body["quantity"] == "10"
    assert sell_response.order_id == "230101000000002"
    assert sell_response.broker == BrokerName.ANGEL_ONE


async def test_place_order_business_rejection_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={"status": False, "message": "Insufficient funds", "errorcode": "AB1004"},
        )

    adapter = AngelOneAdapter(
        make_credentials(),
        make_fast_settings(),
        http_client=make_client(handler),
        instrument_resolver=_FakeResolver(),
    )
    adapter._access_token = "tok"
    order = OrderRequest(
        tradingsymbol="INFY-EQ",
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
                "status": True,
                "data": {"availablecash": 1000.0, "net": 1100.0, "utiliseddebits": 100.0},
            },
        )

    adapter = AngelOneAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    funds = await adapter.get_funds()

    assert calls["n"] > 1
    assert funds.available_cash == 1000.0
    assert funds.total_balance == 1100.0


async def test_http_200_expired_token_triggers_refresh_then_retries_successfully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SmartAPI's distinctive HTTP-200-but-expired signal must trigger the
    same refresh-then-retry flow as a bare 401/403 would.
    """

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                json={"status": False, "message": "Token expired", "errorcode": "AG8001"},
            )
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {"availablecash": 50.0, "net": 55.0, "utiliseddebits": 5.0},
            },
        )

    adapter = AngelOneAdapter(
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


async def test_bare_401_is_also_treated_as_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, text="unauthorized")
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {"availablecash": 1.0, "net": 1.0, "utiliseddebits": 0.0},
            },
        )

    adapter = AngelOneAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "expiring-token"

    async def fake_refresh_token() -> None:
        adapter._access_token = "refreshed-token"

    monkeypatch.setattr(adapter, "refresh_token", fake_refresh_token)

    funds = await adapter.get_funds()

    assert calls["n"] == 2
    assert funds.available_cash == 1.0


async def test_non_auth_status_false_is_not_treated_as_expired() -> None:
    """A `status: false` body without an `AG8*` errorcode is a business
    error, not a token-expiry signal, and must not trigger a refresh.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": False, "message": "no data", "errorcode": "AB0000"}
        )

    adapter = AngelOneAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    # get_funds is not order-placement-specific rejection handling, so this
    # simply exercises that _is_token_expired returns False (no retry loop
    # triggered) and the KeyError from missing "data" propagates normally,
    # proving no TokenExpiredError/refresh was raised in between.
    with pytest.raises(KeyError):
        await adapter.get_funds()


# -- market data ----------------------------------------------------------------------


async def test_ltp_parses_last_price() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/secure/angelbroking/order/v1/getLtpData"
        body = json.loads(request.content.decode())
        assert body == {"exchange": "NSE", "tradingsymbol": "INFY-EQ", "symboltoken": "1594"}
        return httpx.Response(200, json={"status": True, "data": {"ltp": 1550.25}})

    adapter = AngelOneAdapter(
        make_credentials(),
        make_fast_settings(),
        http_client=make_client(handler),
        instrument_resolver=_FakeResolver({(Exchange.NSE, "INFY-EQ"): "1594"}),
    )
    adapter._access_token = "tok"

    quote = await adapter.ltp(Exchange.NSE, "INFY-EQ")

    assert quote.tradingsymbol == "INFY-EQ"
    assert quote.exchange == Exchange.NSE
    assert quote.last_price == 1550.25


async def test_historical_data_parses_candles() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/secure/angelbroking/historical/v1/getCandleData"
        body = json.loads(request.content.decode())
        assert body["symboltoken"] == "1594"
        assert body["interval"] == "ONE_DAY"
        assert body["fromdate"] == "2024-01-01 09:15"
        assert body["todate"] == "2024-01-02 15:30"
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": [
                    ["2024-01-01T09:15:00+05:30", 100.0, 105.0, 99.0, 104.0, 10000],
                    ["2024-01-02T09:15:00+05:30", 104.0, 108.0, 103.0, 107.0, 12000],
                ],
            },
        )

    adapter = AngelOneAdapter(
        make_credentials(),
        make_fast_settings(),
        http_client=make_client(handler),
        instrument_resolver=_FakeResolver({(Exchange.NSE, "INFY-EQ"): "1594"}),
    )
    adapter._access_token = "tok"

    bars = await adapter.historical_data(
        Exchange.NSE,
        "INFY-EQ",
        HistoricalInterval.ONE_DAY,
        datetime(2024, 1, 1, 9, 15),
        datetime(2024, 1, 2, 15, 30),
    )

    assert len(bars) == 2
    assert bars[0].open == 100.0
    assert bars[0].close == 104.0
    assert bars[0].volume == 10000
    assert bars[1].close == 107.0


async def test_historical_data_raises_broker_api_error_on_status_false() -> None:
    """A `"status": false` business error (bad date range, bad symboltoken,
    etc.) must surface with SmartAPI's own message/errorcode, not be
    silently swallowed as "zero candles" the way an empty `"data"` array
    on a `"status": true` response legitimately is.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": False, "message": "Invalid Date Format", "errorcode": "AB1004"},
        )

    adapter = AngelOneAdapter(
        make_credentials(),
        make_fast_settings(),
        http_client=make_client(handler),
        instrument_resolver=_FakeResolver({(Exchange.NSE, "INFY-EQ"): "1594"}),
    )
    adapter._access_token = "tok"

    with pytest.raises(BrokerAPIError, match="Invalid Date Format") as exc_info:
        await adapter.historical_data(
            Exchange.NSE,
            "INFY-EQ",
            HistoricalInterval.ONE_DAY,
            datetime(2024, 1, 1, 9, 15),
            datetime(2024, 1, 2, 15, 30),
        )
    assert exc_info.value.error_code == "AB1004"


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
    """Fakes `websockets.asyncio.client.connect`'s async-context-manager return value.

    Records whatever kwargs (`additional_headers`, etc.) `connect()` was
    called with, so tests can assert the adapter's `headers_factory` was
    actually wired through to the shared `ReconnectingWebSocketClient`.
    """

    def __init__(self, connection: _FakeConnection, captured_kwargs: dict[str, object]) -> None:
        self._connection = connection
        self._captured_kwargs = captured_kwargs

    async def __aenter__(self) -> _FakeConnection:
        return self._connection

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


async def test_start_websocket_subscribes_with_headers_and_parses_ltp_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tradingsymbol = "RELIANCE-EQ"
    token = "2885"
    ltp_paise = 155025  # 1550.25 rupees
    timestamp_ms = 1_700_000_000_000

    frame = make_tick_frame(token, sequence=42, timestamp_ms=timestamp_ms, ltp_paise=ltp_paise)
    connection = _FakeConnection(frame)

    captured_connect_kwargs: dict[str, object] = {}

    def fake_connect(url: str, **kwargs: object) -> _FakeConnectCM:
        captured_connect_kwargs.update(kwargs)
        return _FakeConnectCM(connection, captured_connect_kwargs)

    monkeypatch.setattr("app.brokers.ws_client.connect", fake_connect)

    received: list[Quote] = []

    async def on_tick(quote: Quote) -> None:
        received.append(quote)

    creds = make_credentials()
    adapter = AngelOneAdapter(
        creds,
        make_fast_settings(),
        http_client=make_client(lambda r: httpx.Response(200, json={})),
        instrument_resolver=_FakeResolver({(Exchange.NSE, tradingsymbol): token}),
    )
    adapter._access_token = "tok"
    adapter._feed_token = "feedtok"

    await adapter.start_websocket(on_tick, [(Exchange.NSE, tradingsymbol)])
    try:
        for _ in range(50):
            await asyncio.sleep(0)
            if received:
                break
    finally:
        await adapter.stop_websocket()

    assert len(connection.sent) >= 1
    subscribe_message = json.loads(connection.sent[0])
    assert subscribe_message["action"] == 1
    assert subscribe_message["params"]["mode"] == 1
    # Subscribes with the *resolved* numeric token, not the human symbol.
    assert subscribe_message["params"]["tokenList"] == [{"exchangeType": 1, "tokens": [token]}]

    assert captured_connect_kwargs["additional_headers"] == {
        "Authorization": "Bearer tok",
        "x-api-key": creds.api_key,
        "x-client-code": creds.client_code,
        "x-feed-token": "feedtok",
    }

    assert len(received) == 1
    # The emitted Quote reports back the original tradingsymbol the caller
    # subscribed with, not the raw token decoded off the wire.
    assert received[0].tradingsymbol == tradingsymbol
    assert received[0].exchange == Exchange.NSE
    assert received[0].last_price == 1550.25
    assert received[0].timestamp is not None


async def test_start_websocket_falls_back_to_raw_token_for_unrecognized_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick for a token this call didn't subscribe to (extra/duplicate
    frames from the shared stream) still reaches `on_tick`, reporting the
    raw token since there's no original symbol to map it back to.
    """

    subscribed_token = "2885"
    unrelated_token = "9999"

    frame = make_tick_frame(unrelated_token)
    connection = _FakeConnection(frame)

    def fake_connect(url: str, **kwargs: object) -> _FakeConnectCM:
        return _FakeConnectCM(connection, {})

    monkeypatch.setattr("app.brokers.ws_client.connect", fake_connect)

    received: list[Quote] = []

    async def on_tick(quote: Quote) -> None:
        received.append(quote)

    adapter = AngelOneAdapter(
        make_credentials(),
        make_fast_settings(),
        http_client=make_client(lambda r: httpx.Response(200, json={})),
        instrument_resolver=_FakeResolver({(Exchange.NSE, "RELIANCE-EQ"): subscribed_token}),
    )
    adapter._access_token = "tok"
    adapter._feed_token = "feedtok"

    await adapter.start_websocket(on_tick, [(Exchange.NSE, "RELIANCE-EQ")])
    try:
        for _ in range(50):
            await asyncio.sleep(0)
            if received:
                break
    finally:
        await adapter.stop_websocket()

    assert len(received) == 1
    assert received[0].tradingsymbol == unrelated_token


# -- positions / holdings / order book -------------------------------------------------


async def test_get_positions_parses_response_and_skips_unknown_product() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/secure/angelbroking/order/v1/getPosition"
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": [
                    {
                        "tradingsymbol": "INFY-EQ",
                        "exchange": "NSE",
                        "producttype": "INTRADAY",
                        "netqty": "10",
                        "avgnetprice": "1500.0",
                        "ltp": "1550.0",
                    },
                    {
                        "tradingsymbol": "TCS-EQ",
                        "exchange": "NSE",
                        "producttype": "SOMETHING_UNKNOWN",
                        "netqty": "5",
                        "avgnetprice": "3000.0",
                        "ltp": "3100.0",
                    },
                ],
            },
        )

    adapter = AngelOneAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    positions = await adapter.get_positions()

    assert len(positions) == 1
    assert positions[0].tradingsymbol == "INFY-EQ"
    assert positions[0].product == ProductType.INTRADAY
    assert positions[0].quantity == 10
    assert positions[0].pnl == pytest.approx(500.0)  # (1550 - 1500) * 10, no "pnl" field supplied


async def test_get_positions_prefers_broker_supplied_pnl() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": [
                    {
                        "tradingsymbol": "INFY-EQ",
                        "exchange": "NSE",
                        "producttype": "INTRADAY",
                        "netqty": "10",
                        "avgnetprice": "1500.0",
                        "ltp": "1550.0",
                        "pnl": "999.0",
                    }
                ],
            },
        )

    adapter = AngelOneAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    positions = await adapter.get_positions()

    assert positions[0].pnl == 999.0


async def test_get_holdings_parses_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/secure/angelbroking/portfolio/v1/getHolding"
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": [
                    {
                        "tradingsymbol": "INFY-EQ",
                        "exchange": "NSE",
                        "isin": "INE009A01021",
                        "quantity": "20",
                        "averageprice": "1400.0",
                        "ltp": "1550.0",
                    }
                ],
            },
        )

    adapter = AngelOneAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    holdings = await adapter.get_holdings()

    assert len(holdings) == 1
    assert holdings[0].isin == "INE009A01021"
    assert holdings[0].quantity == 20
    assert holdings[0].pnl == pytest.approx(3000.0)  # (1550 - 1400) * 20


async def test_get_orders_parses_response_and_skips_unknown_product() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/secure/angelbroking/order/v1/getOrderBook"
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": [
                    {
                        "orderid": "230101000000001",
                        "tradingsymbol": "INFY-EQ",
                        "exchange": "NSE",
                        "status": "Complete",
                        "transactiontype": "BUY",
                        "ordertype": "LIMIT",
                        "producttype": "INTRADAY",
                        "quantity": "10",
                        "filledshares": "10",
                        "unfilledshares": "0",
                        "price": "1500.0",
                        "averageprice": "1502.5",
                        "updatetime": "01-Jan-2024 09:20:00",
                    },
                    {
                        "orderid": "230101000000002",
                        "tradingsymbol": "TCS-EQ",
                        "exchange": "NSE",
                        "status": "rejected",
                        "transactiontype": "SELL",
                        "ordertype": "MARKET",
                        "producttype": "SOMETHING_UNKNOWN",
                        "quantity": "5",
                        "filledshares": "0",
                        "unfilledshares": "5",
                        "price": "0",
                        "averageprice": "0",
                        "updatetime": None,
                    },
                ],
            },
        )

    adapter = AngelOneAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    orders = await adapter.get_orders()

    assert len(orders) == 1
    assert orders[0].order_id == "230101000000001"
    assert orders[0].status == OrderStatus.COMPLETE
    assert orders[0].order_timestamp == datetime(2024, 1, 1, 9, 20, 0)


async def test_get_orders_tolerates_unparseable_timestamp() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": [
                    {
                        "orderid": "1",
                        "tradingsymbol": "INFY-EQ",
                        "exchange": "NSE",
                        "status": "open",
                        "transactiontype": "BUY",
                        "ordertype": "MARKET",
                        "producttype": "INTRADAY",
                        "quantity": "1",
                        "filledshares": "0",
                        "unfilledshares": "1",
                        "price": "0",
                        "averageprice": "0",
                        "updatetime": "not-a-timestamp",
                    }
                ],
            },
        )

    adapter = AngelOneAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    orders = await adapter.get_orders()

    assert orders[0].order_timestamp is None


# -- modify / cancel order --------------------------------------------------------------


async def test_modify_order_resolves_token_and_parses_response() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"status": True, "data": {"orderid": "230101000000001"}})

    adapter = AngelOneAdapter(
        make_credentials(),
        make_fast_settings(),
        http_client=make_client(handler),
        instrument_resolver=_FakeResolver({(Exchange.NSE, "INFY-EQ"): "1594"}),
    )
    adapter._access_token = "tok"
    order = OrderRequest(
        tradingsymbol="INFY-EQ",
        exchange=Exchange.NSE,
        transaction_type=OrderSide.BUY,
        quantity=10,
        order_type=OrderType.LIMIT,
        product=ProductType.INTRADAY,
        price=1510.0,
    )

    response = await adapter.modify_order("230101000000001", order)

    assert captured["path"] == "/rest/secure/angelbroking/order/v1/modifyOrder"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["orderid"] == "230101000000001"
    assert body["symboltoken"] == "1594"
    assert response.order_id == "230101000000001"


async def test_modify_order_business_rejection_raises_order_rejection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": False, "message": "Order not modifiable", "errorcode": "AB2020"}
        )

    adapter = AngelOneAdapter(
        make_credentials(),
        make_fast_settings(),
        http_client=make_client(handler),
        instrument_resolver=_FakeResolver(),
    )
    adapter._access_token = "tok"
    order = OrderRequest(
        tradingsymbol="INFY-EQ",
        exchange=Exchange.NSE,
        transaction_type=OrderSide.BUY,
        quantity=10,
        order_type=OrderType.LIMIT,
        product=ProductType.INTRADAY,
        price=1510.0,
    )

    with pytest.raises(OrderRejectionError) as exc_info:
        await adapter.modify_order("230101000000001", order)

    assert "Order not modifiable" in str(exc_info.value)


async def test_cancel_order_builds_request_and_parses_response() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"status": True, "data": {"orderid": "230101000000001"}})

    adapter = AngelOneAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    response = await adapter.cancel_order("230101000000001")

    assert captured["path"] == "/rest/secure/angelbroking/order/v1/cancelOrder"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body == {"variety": "NORMAL", "orderid": "230101000000001"}
    assert response.order_id == "230101000000001"


async def test_cancel_order_business_rejection_raises_order_rejection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": False, "message": "Order already cancelled", "errorcode": "AB2021"}
        )

    adapter = AngelOneAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    with pytest.raises(OrderRejectionError) as exc_info:
        await adapter.cancel_order("230101000000001")

    assert "Order already cancelled" in str(exc_info.value)


async def test_place_order_rejects_good_till_cancelled_validity() -> None:
    adapter = AngelOneAdapter(
        make_credentials(),
        make_fast_settings(),
        http_client=make_client(lambda r: httpx.Response(200, json={})),
        instrument_resolver=_FakeResolver(),
    )
    adapter._access_token = "tok"
    order = OrderRequest(
        tradingsymbol="INFY-EQ",
        exchange=Exchange.NSE,
        transaction_type=OrderSide.BUY,
        quantity=1,
        order_type=OrderType.LIMIT,
        product=ProductType.INTRADAY,
        validity=OrderValidity.GOOD_TILL_CANCELLED,
        price=100.0,
    )

    with pytest.raises(BrokerAPIError, match="validity"):
        await adapter.place_order(order)


# -- token-expiry edge case --------------------------------------------------------------


async def test_non_json_200_body_is_not_treated_as_expired() -> None:
    """An undecodable 200 body (e.g. an HTML error page from a proxy) must
    be treated as "not expired", not crash `_is_token_expired`.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    adapter = AngelOneAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    adapter._access_token = "tok"

    with pytest.raises(BrokerAPIError):
        await adapter.get_funds()


async def test_authenticated_call_before_login_raises_clean_authentication_error() -> None:
    """Regression test for a real bug found during live testing:
    `app.main`'s lifespan deliberately never auto-logs in (Zerodha/Upstox
    tokens are single-use), so `_access_token` is `None` the first time a
    request reaches an adapter. `_auth_headers()` used to interpolate that
    into `"Authorization": "Bearer "` (trailing whitespace, since the
    token is empty) — httpx's HTTP/1.1 layer rejects that as an illegal
    header value, surfacing as a confusing `BrokerConnectionError`
    wrapping an `httpx.LocalProtocolError` instead of an actionable
    "log in first" error. The request must never reach that point.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not send a request before authenticating")

    adapter = AngelOneAdapter(
        make_credentials(), make_fast_settings(), http_client=make_client(handler)
    )
    # No adapter._access_token assignment — simulates a fresh, never-logged-in adapter.

    with pytest.raises(BrokerAuthenticationError, match="call login"):
        await adapter.get_funds()


# -- tick parsing edge cases --------------------------------------------------------------


def test_parse_tick_returns_none_for_undersized_frame() -> None:
    assert AngelOneAdapter._parse_tick(b"\x01\x01too-short") is None


def test_parse_tick_defaults_unknown_exchange_type_to_nse() -> None:
    frame = make_tick_frame("2885", exchange_type=99)  # not in _EXCHANGE_TYPE_FROM_ANGEL_ONE

    quote = AngelOneAdapter._parse_tick(frame)

    assert quote is not None
    assert quote.exchange == Exchange.NSE


def test_parse_tick_treats_non_positive_timestamp_as_missing() -> None:
    frame = make_tick_frame("2885", timestamp_ms=0)

    quote = AngelOneAdapter._parse_tick(frame)

    assert quote is not None
    assert quote.timestamp is None


def test_parse_tick_tolerates_out_of_range_timestamp() -> None:
    """Regression test for a real crash found during live testing: a
    timestamp value the OS's C runtime rejects as out of range (observed
    when the token-field width was still wrong, producing garbage
    timestamps) raised `OSError` from `datetime.fromtimestamp` on Windows
    and crashed the whole tick stream via `ReconnectingWebSocketClient`'s
    "any exception -> reconnect" handling. A single bad frame must drop
    only its own timestamp, not take down the stream.
    """

    frame = make_tick_frame("2885", timestamp_ms=456_945_332_657_408)  # observed garbage value

    quote = AngelOneAdapter._parse_tick(frame)

    assert quote is not None
    assert quote.timestamp is None


# -- adapter lifecycle --------------------------------------------------------------------


async def test_close_closes_default_instrument_master_client() -> None:
    adapter = AngelOneAdapter(
        make_credentials(),
        make_fast_settings(),
        http_client=make_client(lambda r: httpx.Response(200, json={})),
    )

    await adapter.close()

    assert adapter._instruments._client.is_closed  # type: ignore[attr-defined]


async def test_close_does_not_close_injected_resolver() -> None:
    """An injected `SymbolResolver` fake has no HTTP client of its own to
    close — `close()` must not assume every resolver is an
    `AngelOneInstrumentMaster` and must not raise for one that isn't.
    """

    adapter = AngelOneAdapter(
        make_credentials(),
        make_fast_settings(),
        http_client=make_client(lambda r: httpx.Response(200, json={})),
        instrument_resolver=_FakeResolver(),
    )

    await adapter.close()  # must not raise
