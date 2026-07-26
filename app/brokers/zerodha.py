"""Zerodha Kite Connect v3 broker adapter.

Implements `BrokerInterface` against Kite Connect's REST API
(`https://api.kite.trade`) and its binary WebSocket ticker
(`wss://ws.kite.trade`). All shared plumbing — HTTP retry, 5xx/network
error classification, token-expiry-then-refresh-then-retry, and the
reconnecting WebSocket loop — is inherited unchanged from
`BaseBrokerAdapter` / `app.brokers.ws_client`; this module only shapes
Kite-specific requests and responses.

Two structural quirks of Kite Connect that shape this file:

* There is no password-based programmatic login. The user completes a
  browser login and Kite redirects back with a one-time `request_token`,
  which `login()` exchanges for an `access_token`. Standard (non-
  "extended") Kite Connect apps are never issued a `refresh_token`, so
  `refresh_token()` can only succeed for extended apps; otherwise it
  raises `BrokerAuthenticationError` explaining a fresh browser login is
  required.
* Kite's historical-candle and WebSocket-subscribe endpoints key
  instruments by a numeric `instrument_token` from Kite's separate
  instrument-master CSV dump, not by trading symbol. This adapter does
  not implement an instrument-master lookup service, so `historical_data`
  and `start_websocket` both require callers to pass that numeric token
  (as a string) in place of a trading symbol — see the docstrings below.
"""

import hashlib
import json
import struct
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import httpx
import structlog
from websockets.asyncio.client import ClientConnection

from app.brokers.base import BaseBrokerAdapter, TickHandler
from app.config.settings import BrokerSettings, ZerodhaCredentials
from app.domain.entities.broker import (
    BrokerFunds,
    BrokerProfile,
    HistoricalBar,
    Holding,
    OrderDetail,
    OrderRequest,
    OrderResponse,
    Position,
    Quote,
    TokenBundle,
)
from app.domain.enums.trading import (
    BrokerName,
    Exchange,
    HistoricalInterval,
    OrderSide,
    OrderStatus,
    OrderType,
    OrderVariety,
    ProductType,
)
from app.domain.exceptions.broker import (
    BrokerAPIError,
    BrokerAuthenticationError,
    OrderRejectionError,
)

logger = structlog.get_logger(__name__)

#: HTTP status Kite uses for both "forbidden" and "token expired/invalid".
_HTTP_FORBIDDEN = 403

#: Kite's `error_type` value for an expired/invalid access token, returned
#: alongside `_HTTP_FORBIDDEN`.
_TOKEN_EXPIRED_ERROR_TYPE = "TokenException"

#: Kite product codes <-> canonical `ProductType`. Kite has no equivalent
#: for `COVER_ORDER`/`BRACKET_ORDER` as a *product* (those are varieties),
#: so only the three margin products are mapped.
_PRODUCT_FROM_KITE: dict[str, ProductType] = {
    "CNC": ProductType.DELIVERY,
    "MIS": ProductType.INTRADAY,
    "NRML": ProductType.MARGIN,
}
_PRODUCT_TO_KITE: dict[ProductType, str] = {v: k for k, v in _PRODUCT_FROM_KITE.items()}

#: Canonical `OrderVariety` <-> Kite's URL path segment.
_VARIETY_TO_KITE: dict[OrderVariety, str] = {
    OrderVariety.REGULAR: "regular",
    OrderVariety.AFTER_MARKET: "amo",
    OrderVariety.STOP_LOSS: "co",
    OrderVariety.ICEBERG: "iceberg",
}

#: Canonical `OrderType` <-> Kite's order_type string.
_ORDER_TYPE_TO_KITE: dict[OrderType, str] = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.STOP_LOSS: "SL",
    OrderType.STOP_LOSS_MARKET: "SL-M",
}
_ORDER_TYPE_FROM_KITE: dict[str, OrderType] = {v: k for k, v in _ORDER_TYPE_TO_KITE.items()}

#: Kite order-book `status` strings <-> canonical `OrderStatus`. Any value
#: not present here maps to `OrderStatus.UNKNOWN` rather than raising, since
#: Kite's status vocabulary is broader (partial fills, various rejections).
_ORDER_STATUS_FROM_KITE: dict[str, OrderStatus] = {
    "COMPLETE": OrderStatus.COMPLETE,
    "OPEN": OrderStatus.OPEN,
    "CANCELLED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
    "TRIGGER PENDING": OrderStatus.TRIGGER_PENDING,
}

#: Number of packets in a Kite binary tick frame is a 2-byte big-endian
#: unsigned int at the start of the frame.
_PACKET_COUNT_HEADER_BYTES = 2
#: Each packet is prefixed by its own 2-byte big-endian length.
_PACKET_LENGTH_HEADER_BYTES = 2
#: An LTP-mode packet body is exactly 8 bytes: int32 instrument token +
#: int32 last price in paise.
_LTP_PACKET_BODY_BYTES = 8
#: Kite prices in LTP tick packets are integer paise; divide by this to
#: get rupees.
_PAISE_PER_RUPEE = 100.0


class ZerodhaAdapter(BaseBrokerAdapter):
    """`BrokerInterface` implementation for Zerodha Kite Connect v3.

    Only broker-specific request/response shaping lives here; retries,
    5xx/network error classification, and the token-expiry-refresh-retry
    flow are all inherited from `BaseBrokerAdapter`.
    """

    def __init__(
        self,
        credentials: ZerodhaCredentials,
        broker_settings: BrokerSettings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            broker_name=BrokerName.ZERODHA,
            base_url=credentials.base_url,
            request_timeout_seconds=broker_settings.request_timeout_seconds,
            max_retries=broker_settings.max_retries,
            retry_backoff_seconds=broker_settings.retry_backoff_seconds,
            retry_max_backoff_seconds=broker_settings.retry_max_backoff_seconds,
            http_client=http_client,
        )
        self._credentials = credentials
        self._access_token: str | None = None

    # -- Auth hooks required by BaseBrokerAdapter ---------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Kite Connect's authenticated-request headers.

        `access_token` is `""` before the first successful `login()`; Kite
        will reject such a request with a `TokenException`, which the base
        class's retry-on-token-expiry flow already handles.
        """

        return {
            "Authorization": f"token {self._credentials.api_key}:{self._access_token or ''}",
            "X-Kite-Version": "3",
        }

    def _is_token_expired(self, response: httpx.Response) -> bool:
        """Kite signals an expired/invalid access token as HTTP 403 with
        a JSON body carrying `"error_type": "TokenException"`.
        """

        if response.status_code != _HTTP_FORBIDDEN:
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        return isinstance(body, dict) and body.get("error_type") == _TOKEN_EXPIRED_ERROR_TYPE

    @staticmethod
    def _checksum(*parts: str) -> str:
        """Kite's login/refresh checksum: `sha256(part1 + part2 + ...)` hex digest."""

        return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()

    # -- Auth -----------------------------------------------------------------

    async def login(self) -> TokenBundle:
        """Exchange the browser-obtained `request_token` for an access token.

        Kite Connect has no password-based programmatic login: this only
        works once per `request_token`, which the user must obtain by
        completing Kite's browser login redirect beforehand.
        """

        api_key = self._credentials.api_key
        request_token = self._credentials.request_token
        checksum = self._checksum(api_key, request_token, self._credentials.api_secret)

        body = await self._request(
            "POST",
            "/session/token",
            authenticated=False,
            retry_on_token_expiry=False,
            data={"api_key": api_key, "request_token": request_token, "checksum": checksum},
        )
        data = body["data"]
        self._access_token = data["access_token"]
        return TokenBundle(
            access_token=data["access_token"], refresh_token=data.get("refresh_token")
        )

    async def refresh_token(self) -> TokenBundle:
        """Exchange a refresh token for a new access token.

        Standard Kite Connect apps are never issued a `refresh_token` — only
        Kite's "extended" apps are — and `ZerodhaCredentials` has no field to
        hold one since this codebase targets a standard app. There is
        therefore no programmatic recovery from an expired access token: the
        caller must repeat the browser login flow to obtain a fresh
        `request_token` and call `login()` again. This method always raises
        rather than fabricate a refresh request Kite would reject anyway.
        """

        raise BrokerAuthenticationError(
            "zerodha: no refresh_token is available for this session; standard Kite Connect "
            "apps are not issued one, so a fresh browser login (new request_token) is required "
            "before calling login() again",
            broker=self._broker_name,
        )

    # -- Account/profile --------------------------------------------------------

    async def get_profile(self) -> BrokerProfile:
        """Fetch the logged-in account's profile and entitlements."""

        body = await self._request("GET", "/user/profile")
        data = body["data"]

        exchanges: list[Exchange] = []
        for raw_exchange in data.get("exchanges", []):
            try:
                exchanges.append(Exchange(raw_exchange))
            except ValueError:
                logger.debug("zerodha_unknown_exchange", value=raw_exchange)

        products: list[ProductType] = []
        for raw_product in data.get("products", []):
            product = _PRODUCT_FROM_KITE.get(raw_product)
            if product is not None:
                products.append(product)
            else:
                logger.debug("zerodha_unknown_product", value=raw_product)

        return BrokerProfile(
            broker=BrokerName.ZERODHA,
            client_id=data["user_id"],
            display_name=data["user_name"],
            email=data.get("email"),
            exchanges_enabled=tuple(exchanges),
            products_enabled=tuple(products),
        )

    async def get_funds(self) -> BrokerFunds:
        """Fetch available equity-segment margin/cash."""

        body = await self._request("GET", "/user/margins")
        equity = body["data"]["equity"]
        return BrokerFunds(
            broker=BrokerName.ZERODHA,
            available_cash=float(equity["available"]["cash"]),
            used_margin=float(equity["utilised"]["debits"]),
            total_balance=float(equity["net"]),
            raw=equity,
        )

    async def get_positions(self) -> list[Position]:
        """Fetch open intraday/carry-forward positions (Kite's `net` book)."""

        body = await self._request("GET", "/portfolio/positions")
        positions: list[Position] = []
        for item in body["data"]["net"]:
            product = _PRODUCT_FROM_KITE.get(item["product"])
            if product is None:
                logger.debug("zerodha_unknown_product", value=item["product"])
                continue
            positions.append(
                Position(
                    tradingsymbol=item["tradingsymbol"],
                    exchange=Exchange(item["exchange"]),
                    product=product,
                    quantity=int(item["quantity"]),
                    average_price=float(item["average_price"]),
                    last_price=float(item["last_price"]),
                    pnl=float(item["pnl"]),
                )
            )
        return positions

    async def get_holdings(self) -> list[Holding]:
        """Fetch settled (T+1) demat holdings."""

        body = await self._request("GET", "/portfolio/holdings")
        return [
            Holding(
                tradingsymbol=item["tradingsymbol"],
                exchange=Exchange(item["exchange"]),
                isin=item["isin"],
                quantity=int(item["quantity"]),
                average_price=float(item["average_price"]),
                last_price=float(item["last_price"]),
                pnl=float(item["pnl"]),
            )
            for item in body["data"]
        ]

    async def get_orders(self) -> list[OrderDetail]:
        """Fetch today's order book."""

        body = await self._request("GET", "/orders")
        orders: list[OrderDetail] = []
        for item in body["data"]:
            product = _PRODUCT_FROM_KITE.get(item["product"])
            if product is None:
                logger.debug("zerodha_unknown_product", value=item["product"])
                continue
            order_timestamp = self._parse_timestamp(item.get("order_timestamp"))
            orders.append(
                OrderDetail(
                    order_id=item["order_id"],
                    tradingsymbol=item["tradingsymbol"],
                    exchange=Exchange(item["exchange"]),
                    status=_ORDER_STATUS_FROM_KITE.get(item["status"], OrderStatus.UNKNOWN),
                    transaction_type=OrderSide(item["transaction_type"]),
                    order_type=_ORDER_TYPE_FROM_KITE.get(item["order_type"], OrderType.MARKET),
                    product=product,
                    quantity=int(item["quantity"]),
                    filled_quantity=int(item["filled_quantity"]),
                    pending_quantity=int(item["pending_quantity"]),
                    price=float(item["price"]),
                    average_price=float(item["average_price"]),
                    order_timestamp=order_timestamp,
                    status_message=item.get("status_message"),
                )
            )
        return orders

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        """Parse Kite's `"YYYY-MM-DD HH:MM:SS"` timestamps, tolerating `None`."""

        if not value:
            return None
        return datetime.fromisoformat(value)

    # -- Orders -----------------------------------------------------------------

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """Place a new order via `POST /orders/{variety}`."""

        variety = _VARIETY_TO_KITE[order.variety]
        payload = self._order_payload(order)
        try:
            body = await self._request("POST", f"/orders/{variety}", data=payload)
        except BrokerAPIError as exc:
            raise self._as_order_rejection(exc) from exc
        return OrderResponse(
            order_id=body["data"]["order_id"], broker=BrokerName.ZERODHA, raw=body["data"]
        )

    async def modify_order(self, order_id: str, order: OrderRequest) -> OrderResponse:
        """Modify an existing, still-open order via `PUT /orders/{variety}/{order_id}`."""

        variety = _VARIETY_TO_KITE[order.variety]
        payload: dict[str, Any] = {
            "quantity": order.quantity,
            "order_type": _ORDER_TYPE_TO_KITE[order.order_type],
            "validity": order.validity.value,
        }
        if order.price is not None:
            payload["price"] = order.price
        if order.trigger_price is not None:
            payload["trigger_price"] = order.trigger_price

        try:
            body = await self._request("PUT", f"/orders/{variety}/{order_id}", data=payload)
        except BrokerAPIError as exc:
            raise self._as_order_rejection(exc) from exc
        return OrderResponse(
            order_id=body["data"]["order_id"], broker=BrokerName.ZERODHA, raw=body["data"]
        )

    async def cancel_order(
        self, order_id: str, variety: OrderVariety = OrderVariety.REGULAR
    ) -> OrderResponse:
        """Cancel an existing, still-open order via `DELETE /orders/{variety}/{order_id}`."""

        kite_variety = _VARIETY_TO_KITE[variety]
        try:
            body = await self._request("DELETE", f"/orders/{kite_variety}/{order_id}")
        except BrokerAPIError as exc:
            raise self._as_order_rejection(exc) from exc
        return OrderResponse(
            order_id=body["data"]["order_id"], broker=BrokerName.ZERODHA, raw=body["data"]
        )

    @staticmethod
    def _order_payload(order: OrderRequest) -> dict[str, Any]:
        """Build the form-encoded body Kite expects for `place_order`."""

        payload: dict[str, Any] = {
            "tradingsymbol": order.tradingsymbol,
            "exchange": order.exchange.value,
            "transaction_type": order.transaction_type.value,
            "order_type": _ORDER_TYPE_TO_KITE[order.order_type],
            "quantity": order.quantity,
            "product": _PRODUCT_TO_KITE[order.product],
            "validity": order.validity.value,
        }
        if order.price is not None:
            payload["price"] = order.price
        if order.trigger_price is not None:
            payload["trigger_price"] = order.trigger_price
        if order.tag is not None:
            payload["tag"] = order.tag
        return payload

    def _as_order_rejection(self, exc: BrokerAPIError) -> OrderRejectionError:
        """Re-raise a generic `BrokerAPIError` from an order call as an
        `OrderRejectionError`, preferring Kite's `message` field if present.

        `BaseBrokerAdapter._send_once` formats `BrokerAPIError`'s message as
        `"{broker}: request to {url} failed with {status_code}: {response.text}"`;
        the raw response body is recovered here by splitting on that known
        marker rather than re-fetching the response, since only the
        exception (not the `httpx.Response`) crosses the `_request` boundary.
        """

        marker = f"failed with {exc.status_code}: "
        _, _, raw_body = str(exc).partition(marker)
        parsed_body: object = None
        if raw_body:
            try:
                parsed_body = json.loads(raw_body)
            except ValueError:
                parsed_body = None

        message = f"{self._broker_name.value}: order rejected: {exc}"
        if isinstance(parsed_body, dict) and parsed_body.get("message"):
            message = f"{self._broker_name.value}: order rejected: {parsed_body['message']}"
        return OrderRejectionError(message, broker=self._broker_name, status_code=exc.status_code)

    # -- Market data --------------------------------------------------------------

    async def ltp(self, exchange: Exchange, tradingsymbol: str) -> Quote:
        """Fetch a last-traded-price snapshot via `GET /quote/ltp`."""

        instrument_key = f"{exchange.value}:{tradingsymbol}"
        body = await self._request("GET", "/quote/ltp", params={"i": instrument_key})
        last_price = body["data"][instrument_key]["last_price"]
        return Quote(
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            last_price=float(last_price),
            timestamp=None,
        )

    async def historical_data(
        self,
        exchange: Exchange,
        tradingsymbol: str,
        interval: HistoricalInterval,
        from_date: datetime,
        to_date: datetime,
    ) -> list[HistoricalBar]:
        """Fetch historical OHLCV candles via `GET /instruments/historical/...`.

        Caveat: Kite Connect's historical-candle endpoint is actually keyed
        by a numeric `instrument_token` from Kite's separate instrument-master
        CSV dump (refreshed daily), not by a plain trading symbol. Resolving
        that symbol -> token mapping is out of scope for this adapter — until
        an instrument-master lookup service exists, callers must pass the
        numeric instrument token (as a string) via the `tradingsymbol`
        parameter.
        """

        body = await self._request(
            "GET",
            f"/instruments/historical/{tradingsymbol}/{interval.value}",
            params={"from": from_date.strftime("%Y-%m-%d"), "to": to_date.strftime("%Y-%m-%d")},
        )
        candles = body["data"]["candles"]
        return [
            HistoricalBar(
                timestamp=datetime.fromisoformat(candle[0]),
                open=float(candle[1]),
                high=float(candle[2]),
                low=float(candle[3]),
                close=float(candle[4]),
                volume=int(candle[5]),
            )
            for candle in candles
        ]

    # -- WebSocket streaming --------------------------------------------------------

    async def start_websocket(
        self, on_tick: TickHandler, instruments: Sequence[tuple[Exchange, str]]
    ) -> None:
        """Open Kite's ticker WebSocket and subscribe in LTP mode.

        Caveat (see `historical_data`): Kite's ticker subscribes by numeric
        `instrument_token`, not trading symbol, so each `str` in
        `instruments` is treated as that token, not a plain symbol.

        Caveat: the binary tick-packet layout parsed in `_parse_ticks` is
        Kite's documented LTP-mode packet shape as of this writing (a
        2-byte packet count, then per packet a 2-byte length followed by
        an 8-byte body of `int32 instrument_token, int32 price_in_paise`).
        This must be re-verified against Kite Connect's current official
        binary protocol documentation before production use — it is not
        guaranteed to be exactly correct or stable across Kite API versions.
        """

        tokens = [tradingsymbol for _, tradingsymbol in instruments]

        async def url_factory() -> str:
            return (
                f"wss://ws.kite.trade?api_key={self._credentials.api_key}"
                f"&access_token={self._access_token or ''}"
            )

        async def on_connect(connection: ClientConnection) -> None:
            await connection.send(json.dumps({"a": "subscribe", "v": tokens}))
            await connection.send(json.dumps({"a": "mode", "v": ["ltp", tokens]}))

        async def on_message(message: str | bytes) -> None:
            if not isinstance(message, bytes):
                return  # Kite's ticker only sends JSON text for postbacks/errors, not ticks.
            for quote in self._parse_ticks(message):
                await on_tick(quote)

        await self._start_ws(url_factory, on_message, on_connect)

    @staticmethod
    def _parse_ticks(frame: bytes) -> list[Quote]:
        """Best-effort decode of a Kite LTP-mode binary tick frame.

        See the production-readiness caveat in `start_websocket`'s
        docstring — this layout must be reconfirmed against Kite's current
        binary protocol docs.
        """

        quotes: list[Quote] = []
        if len(frame) < _PACKET_COUNT_HEADER_BYTES:
            return quotes

        (packet_count,) = struct.unpack_from(">H", frame, 0)
        offset = _PACKET_COUNT_HEADER_BYTES
        for _ in range(packet_count):
            if offset + _PACKET_LENGTH_HEADER_BYTES > len(frame):
                break
            (packet_length,) = struct.unpack_from(">H", frame, offset)
            offset += _PACKET_LENGTH_HEADER_BYTES

            packet = frame[offset : offset + packet_length]
            offset += packet_length

            if packet_length != _LTP_PACKET_BODY_BYTES:
                continue  # Not an LTP-mode packet (quote/full mode carries more fields); skip.

            instrument_token, price_paise = struct.unpack(">ii", packet)
            quotes.append(
                Quote(
                    tradingsymbol=str(instrument_token),
                    exchange=Exchange.NSE,
                    last_price=price_paise / _PAISE_PER_RUPEE,
                    timestamp=None,
                )
            )
        return quotes
