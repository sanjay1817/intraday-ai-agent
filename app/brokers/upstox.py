"""Upstox API v2 broker adapter.

Implements `BrokerInterface` against Upstox's REST API
(`https://api.upstox.com/v2`) and its market-data WebSocket feed. All
shared plumbing — HTTP retry, 5xx/network error classification,
token-expiry-then-refresh-then-retry, and the reconnecting WebSocket loop
— is inherited unchanged from `BaseBrokerAdapter` / `app.brokers.ws_client`;
this module only shapes Upstox-specific requests and responses.

Several structural quirks of Upstox v2 shape this file:

* There is no password-based programmatic login. The user completes a
  browser OAuth2 authorization redirect and Upstox redirects back with a
  one-time `code`, which `login()` exchanges (via a form-urlencoded POST)
  for an `access_token`. Upstox's standard flow issues no refresh token,
  so `refresh_token()` always raises `BrokerAuthenticationError` — a
  fresh browser authorization is the only recovery path.
* Order placement, market-quote, and historical-candle endpoints key
  instruments by an `instrument_key` such as `"NSE_EQ|INE848E01016"`
  (exchange segment + ISIN), not a plain trading symbol. This adapter
  does not implement an instrument-master lookup service, so it builds a
  **placeholder-shaped** key `f"{exchange.value}|{tradingsymbol}"` (see
  `_instrument_key`) wherever Upstox requires one. This is not a real
  Upstox instrument key and must be replaced by a genuine instrument-master
  lookup before production use.
* Upstox v2's dated historical-candle endpoint only serves `"day"` /
  `"week"` / `"month"` candles; any sub-day interval must instead be
  fetched from the separate `intraday-candle` endpoint, which only ever
  returns the current trading day's data. `historical_data` routes
  between the two accordingly.
* Upstox v2's real market-data feed is protobuf-encoded, requiring
  Upstox's official `.proto` schema (not vendored here) to decode. This
  adapter implements the feed-authorization REST call and a best-effort
  JSON subscribe/decode path for completeness, but cannot decode binary
  (protobuf) frames — see `start_websocket`'s docstring for the exact
  limitation.
"""

import json
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import httpx
import structlog
from websockets.asyncio.client import ClientConnection

from app.brokers.base import BaseBrokerAdapter, TickHandler
from app.config.settings import BrokerSettings, UpstoxCredentials
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
    OrderValidity,
    OrderVariety,
    ProductType,
)
from app.domain.exceptions.broker import (
    BrokerAPIError,
    BrokerAuthenticationError,
    OrderRejectionError,
)

logger = structlog.get_logger(__name__)

#: HTTP status Upstox uses for an expired/invalid access token. Any 401 is
#: treated as expired regardless of body shape (see `_is_token_expired`).
_HTTP_UNAUTHORIZED = 401

#: Upstox product codes <-> canonical `ProductType`. `MTF` (margin trading
#: facility) is Upstox's margin product; bracket orders are not exposed as
#: a distinct *product* by Upstox v2, so `BRACKET_ORDER` has no mapping.
_PRODUCT_FROM_UPSTOX: dict[str, ProductType] = {
    "D": ProductType.DELIVERY,
    "I": ProductType.INTRADAY,
    "MTF": ProductType.MARGIN,
    "CO": ProductType.COVER_ORDER,
}
_PRODUCT_TO_UPSTOX: dict[ProductType, str] = {v: k for k, v in _PRODUCT_FROM_UPSTOX.items()}

#: Canonical `OrderType` <-> Upstox's order_type string.
_ORDER_TYPE_TO_UPSTOX: dict[OrderType, str] = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.STOP_LOSS: "SL",
    OrderType.STOP_LOSS_MARKET: "SL-M",
}
_ORDER_TYPE_FROM_UPSTOX: dict[str, OrderType] = {v: k for k, v in _ORDER_TYPE_TO_UPSTOX.items()}

#: Canonical `OrderValidity` <-> Upstox's validity string. Upstox v2 has no
#: good-till-cancelled order type, so `GOOD_TILL_CANCELLED` is deliberately
#: absent — `place_order`/`modify_order` raise `BrokerAPIError` for it.
_VALIDITY_TO_UPSTOX: dict[OrderValidity, str] = {
    OrderValidity.DAY: "DAY",
    OrderValidity.IMMEDIATE_OR_CANCEL: "IOC",
}

#: Upstox order-book `status` strings (lower-cased before lookup) <->
#: canonical `OrderStatus`. Any value not present here maps to
#: `OrderStatus.UNKNOWN` rather than raising.
_ORDER_STATUS_FROM_UPSTOX: dict[str, OrderStatus] = {
    "complete": OrderStatus.COMPLETE,
    "open": OrderStatus.OPEN,
    "cancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
    "trigger pending": OrderStatus.TRIGGER_PENDING,
}


class UpstoxAdapter(BaseBrokerAdapter):
    """`BrokerInterface` implementation for Upstox API v2.

    Only broker-specific request/response shaping lives here; retries,
    5xx/network error classification, and the token-expiry-refresh-retry
    flow are all inherited from `BaseBrokerAdapter`.
    """

    def __init__(
        self,
        credentials: UpstoxCredentials,
        broker_settings: BrokerSettings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            broker_name=BrokerName.UPSTOX,
            base_url=credentials.base_url,
            request_timeout_seconds=broker_settings.request_timeout_seconds,
            max_retries=broker_settings.max_retries,
            retry_backoff_seconds=broker_settings.retry_backoff_seconds,
            retry_max_backoff_seconds=broker_settings.retry_max_backoff_seconds,
            http_client=http_client,
        )
        self._credentials = credentials
        self._access_token: str | None = None
        #: Ensures the binary-frame-unsupported WebSocket warning is only
        #: logged once per adapter instance rather than once per frame.
        self._warned_binary_frame = False

    # -- Auth hooks required by BaseBrokerAdapter ---------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Upstox's authenticated-request headers (bearer token)."""

        return {
            "Authorization": f"Bearer {self._access_token or ''}",
            "Accept": "application/json",
        }

    def _is_token_expired(self, response: httpx.Response) -> bool:
        """Upstox signals an expired/invalid access token as a bare HTTP 401.

        No JSON body inspection is needed (or attempted): any 401 is
        treated as expired, so a non-JSON or unparseable body can never
        cause this check to fail.
        """

        return response.status_code == _HTTP_UNAUTHORIZED

    @staticmethod
    def _instrument_key(exchange: Exchange, tradingsymbol: str) -> str:
        """Build the `instrument_key` Upstox expects for quote/order/candle calls.

        Upstox's real instrument keys are `"{exchange_segment}|{isin}"`
        (e.g. `"NSE_EQ|INE848E01016"`), resolved from Upstox's separate
        instrument-master dump. That lookup is out of scope for this
        adapter, so this placeholder simply joins the canonical exchange
        and trading symbol (`"NSE|INFY"`) — callers must not assume this
        is a real, resolvable Upstox instrument key.
        """

        return f"{exchange.value}|{tradingsymbol}"

    # -- Auth -----------------------------------------------------------------

    async def login(self) -> TokenBundle:
        """Exchange the browser-obtained OAuth2 `auth_code` for an access token.

        Upstox has no password-based programmatic login: this only works
        once per `auth_code`, which the user must obtain by completing
        Upstox's browser authorization redirect beforehand.
        """

        body = await self._request(
            "POST",
            "/login/authorization/token",
            authenticated=False,
            retry_on_token_expiry=False,
            data={
                "code": self._credentials.auth_code,
                "client_id": self._credentials.api_key,
                "client_secret": self._credentials.api_secret,
                "redirect_uri": self._credentials.redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        self._access_token = body["access_token"]
        return TokenBundle(access_token=body["access_token"])

    async def refresh_token(self) -> TokenBundle:
        """Always raises: Upstox's standard OAuth2 flow issues no refresh token.

        There is no programmatic recovery from an expired access token —
        the caller must repeat the browser authorization redirect to
        obtain a fresh `auth_code` and call `login()` again.
        """

        raise BrokerAuthenticationError(
            "upstox: no programmatic refresh exists for standard access tokens; a fresh browser "
            "authorization (new auth_code) is required, followed by calling login() again",
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
                logger.debug("upstox_unknown_exchange", value=raw_exchange)

        products: list[ProductType] = []
        for raw_product in data.get("products", []):
            product = _PRODUCT_FROM_UPSTOX.get(raw_product)
            if product is not None:
                products.append(product)
            else:
                logger.debug("upstox_unknown_product", value=raw_product)

        return BrokerProfile(
            broker=BrokerName.UPSTOX,
            client_id=data["user_id"],
            display_name=data["user_name"],
            email=data.get("email"),
            exchanges_enabled=tuple(exchanges),
            products_enabled=tuple(products),
        )

    async def get_funds(self) -> BrokerFunds:
        """Fetch available equity-segment margin/cash.

        Upstox's response has no explicit "total" field, so it is
        approximated as `available_margin + used_margin` unless the
        broker does supply one.
        """

        body = await self._request("GET", "/user/get-funds-and-margin")
        equity = body["data"]["equity"]
        available_margin = float(equity["available_margin"])
        used_margin = float(equity["used_margin"])
        total_balance = (
            float(equity["total"]) if "total" in equity else available_margin + used_margin
        )
        return BrokerFunds(
            broker=BrokerName.UPSTOX,
            available_cash=available_margin,
            used_margin=used_margin,
            total_balance=total_balance,
            raw=equity,
        )

    async def get_positions(self) -> list[Position]:
        """Fetch open intraday/carry-forward positions."""

        body = await self._request("GET", "/portfolio/short-term-positions")
        positions: list[Position] = []
        for item in body["data"]:
            product = _PRODUCT_FROM_UPSTOX.get(item["product"])
            if product is None:
                logger.debug("upstox_unknown_product", value=item["product"])
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

        body = await self._request("GET", "/portfolio/long-term-holdings")
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

        body = await self._request("GET", "/order/retrieve-all")
        orders: list[OrderDetail] = []
        for item in body["data"]:
            product = _PRODUCT_FROM_UPSTOX.get(item["product"])
            if product is None:
                logger.debug("upstox_unknown_product", value=item["product"])
                continue
            orders.append(
                OrderDetail(
                    order_id=item["order_id"],
                    tradingsymbol=item["tradingsymbol"],
                    exchange=Exchange(item["exchange"]),
                    status=_ORDER_STATUS_FROM_UPSTOX.get(
                        str(item["status"]).lower(), OrderStatus.UNKNOWN
                    ),
                    transaction_type=OrderSide(item["transaction_type"]),
                    order_type=_ORDER_TYPE_FROM_UPSTOX.get(
                        str(item["order_type"]).upper(), OrderType.MARKET
                    ),
                    product=product,
                    quantity=int(item["quantity"]),
                    filled_quantity=int(item["filled_quantity"]),
                    pending_quantity=int(item["pending_quantity"]),
                    price=float(item["price"]),
                    average_price=float(item["average_price"]),
                    order_timestamp=self._parse_order_timestamp(item.get("order_timestamp")),
                )
            )
        return orders

    @staticmethod
    def _parse_order_timestamp(value: str | None) -> datetime | None:
        """Parse Upstox's order-book timestamp, tolerating `None`/unparseable values."""

        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    # -- Orders -----------------------------------------------------------------

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """Place a new order via `POST /order/place`.

        `instrument_token` is built by `_instrument_key` — see that
        method's docstring for the important caveat that this is a
        placeholder, not a real Upstox instrument key.
        """

        product = self._product_to_upstox(order.product)
        validity = self._validity_to_upstox(order.validity)
        payload: dict[str, Any] = {
            "quantity": order.quantity,
            "product": product,
            "validity": validity,
            "price": order.price if order.price is not None else 0,
            "tag": order.tag,
            "instrument_token": self._instrument_key(order.exchange, order.tradingsymbol),
            "order_type": _ORDER_TYPE_TO_UPSTOX[order.order_type],
            "transaction_type": order.transaction_type.value,
            "disclosed_quantity": 0,
            "trigger_price": order.trigger_price if order.trigger_price is not None else 0,
            "is_amo": order.variety == OrderVariety.AFTER_MARKET,
        }
        try:
            body = await self._request("POST", "/order/place", json=payload)
        except BrokerAPIError as exc:
            raise self._as_order_rejection(exc) from exc
        data = body["data"]
        return OrderResponse(order_id=data["order_id"], broker=BrokerName.UPSTOX, raw=data)

    async def modify_order(self, order_id: str, order: OrderRequest) -> OrderResponse:
        """Modify an existing, still-open order via `PUT /order/modify`."""

        validity = self._validity_to_upstox(order.validity)
        payload: dict[str, Any] = {
            "order_id": order_id,
            "quantity": order.quantity,
            "order_type": _ORDER_TYPE_TO_UPSTOX[order.order_type],
            "price": order.price if order.price is not None else 0,
            "trigger_price": order.trigger_price if order.trigger_price is not None else 0,
            "validity": validity,
        }
        try:
            body = await self._request("PUT", "/order/modify", json=payload)
        except BrokerAPIError as exc:
            raise self._as_order_rejection(exc) from exc
        data = body["data"]
        return OrderResponse(order_id=data["order_id"], broker=BrokerName.UPSTOX, raw=data)

    async def cancel_order(
        self, order_id: str, variety: OrderVariety = OrderVariety.REGULAR
    ) -> OrderResponse:
        """Cancel an existing, still-open order via `DELETE /order/cancel`.

        Upstox's v2 cancel endpoint is keyed by `order_id` alone, so
        `variety` is accepted for interface uniformity but not sent.
        """

        try:
            body = await self._request("DELETE", "/order/cancel", params={"order_id": order_id})
        except BrokerAPIError as exc:
            raise self._as_order_rejection(exc) from exc
        data = body["data"]
        return OrderResponse(order_id=data["order_id"], broker=BrokerName.UPSTOX, raw=data)

    @staticmethod
    def _product_to_upstox(product: ProductType) -> str:
        """Map a canonical `ProductType` to Upstox's product code.

        Raises `BrokerAPIError` for `BRACKET_ORDER`, which Upstox v2 has
        no product code for.
        """

        upstox_product = _PRODUCT_TO_UPSTOX.get(product)
        if upstox_product is None:
            raise BrokerAPIError(
                f"upstox: product {product.value} is not supported for order placement",
                broker=BrokerName.UPSTOX,
            )
        return upstox_product

    @staticmethod
    def _validity_to_upstox(validity: OrderValidity) -> str:
        """Map a canonical `OrderValidity` to Upstox's validity string.

        Raises `BrokerAPIError` for `GOOD_TILL_CANCELLED`, which Upstox
        v2 does not support.
        """

        upstox_validity = _VALIDITY_TO_UPSTOX.get(validity)
        if upstox_validity is None:
            raise BrokerAPIError(
                f"upstox: validity {validity.value} is not supported",
                broker=BrokerName.UPSTOX,
            )
        return upstox_validity

    def _as_order_rejection(self, exc: BrokerAPIError) -> OrderRejectionError:
        """Re-raise a generic `BrokerAPIError` from an order call as an
        `OrderRejectionError`, preferring Upstox's `errors[0].message` field
        if present.

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
        if isinstance(parsed_body, dict):
            errors = parsed_body.get("errors")
            if (
                isinstance(errors, list)
                and errors
                and isinstance(errors[0], dict)
                and errors[0].get("message")
            ):
                message = f"{self._broker_name.value}: order rejected: {errors[0]['message']}"
        return OrderRejectionError(message, broker=self._broker_name, status_code=exc.status_code)

    # -- Market data --------------------------------------------------------------

    async def ltp(self, exchange: Exchange, tradingsymbol: str) -> Quote:
        """Fetch a last-traded-price snapshot via `GET /market-quote/ltp`.

        Upstox keys the response `data` object by a composite string (its
        exact format varies by instrument type), so the first — and only
        — entry is read positionally rather than by reconstructing the key.
        """

        instrument_key = self._instrument_key(exchange, tradingsymbol)
        body = await self._request(
            "GET", "/market-quote/ltp", params={"instrument_key": instrument_key}
        )
        data = body["data"]
        if not data:
            raise BrokerAPIError(
                f"upstox: no quote data returned for {instrument_key}", broker=self._broker_name
            )
        quote_data = next(iter(data.values()))
        return Quote(
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            last_price=float(quote_data["last_price"]),
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
        """Fetch historical OHLCV candles.

        Upstox v2 nuance: the dated `/historical-candle/{key}/{interval}/{to}/{from}`
        endpoint only serves day/week/month candles. Any sub-day
        `HistoricalInterval` is not available through it — Upstox v2 only
        exposes intraday (minute-level) candles for the *current* trading
        day, via the separate `/historical-candle/intraday/{key}/{interval}`
        endpoint, which this method calls instead for those intervals.

        See `_instrument_key`'s docstring for the placeholder-instrument-key
        caveat that also applies here.
        """

        instrument_key = self._instrument_key(exchange, tradingsymbol)
        if interval == HistoricalInterval.ONE_DAY:
            url = f"/historical-candle/{instrument_key}/{interval.value}/{to_date:%Y-%m-%d}/{from_date:%Y-%m-%d}"
        else:
            url = f"/historical-candle/intraday/{instrument_key}/{interval.value}"

        body = await self._request("GET", url)
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
        """Open Upstox's market-data feed and subscribe in LTPC mode.

        Known limitation: Upstox v2's real market-data feed wire protocol
        is protobuf-encoded, and decoding it correctly requires Upstox's
        official `.proto` schema, which is not vendored in this codebase.
        This method implements the parts that are genuine REST/WebSocket
        plumbing — calling `GET /feed/market-data-feed/authorize` to obtain
        a fresh signed `wss://` URL on every (re)connect, and sending a
        best-effort JSON subscribe frame on connect — but `on_message` can
        only decode plain JSON text frames. Any binary (protobuf) frame is
        detected, logged once via a `structlog` warning
        (`"upstox_websocket_binary_frame_unsupported"`), and dropped
        without calling `on_tick`. Decoding the real feed requires adding
        Upstox's official protobuf schema and is out of scope here.

        See `_instrument_key`'s docstring for the placeholder-instrument-key
        caveat that also applies to the subscribed instrument keys.
        """

        instrument_keys = [
            self._instrument_key(exchange, tradingsymbol) for exchange, tradingsymbol in instruments
        ]

        async def url_factory() -> str:
            body = await self._request("GET", "/feed/market-data-feed/authorize")
            return str(body["data"]["authorized_redirect_uri"])

        async def on_connect(connection: ClientConnection) -> None:
            subscribe_message = {
                "guid": str(uuid.uuid4()),
                "method": "sub",
                "data": {"mode": "ltpc", "instrumentKeys": instrument_keys},
            }
            await connection.send(json.dumps(subscribe_message))

        async def on_message(message: str | bytes) -> None:
            try:
                payload = json.loads(message)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                if not self._warned_binary_frame:
                    logger.warning("upstox_websocket_binary_frame_unsupported")
                    self._warned_binary_frame = True
                return
            for quote in self._parse_feed_quotes(payload):
                await on_tick(quote)

        await self._start_ws(url_factory, on_message, on_connect)

    @staticmethod
    def _parse_feed_quotes(payload: object) -> list[Quote]:
        """Best-effort decode of a JSON `{"feeds": {...}}` tick message.

        See the production-readiness caveat in `start_websocket`'s
        docstring — Upstox's real feed is protobuf, not JSON; this only
        handles the hypothetical JSON shape documented there.
        """

        if not isinstance(payload, dict):
            return []
        feeds = payload.get("feeds")
        if not isinstance(feeds, dict):
            return []

        quotes: list[Quote] = []
        for instrument_key, feed in feeds.items():
            if not isinstance(feed, dict):
                continue
            ltpc = feed.get("ltpc")
            if not isinstance(ltpc, dict) or "ltp" not in ltpc:
                continue
            exchange_value, _, tradingsymbol = str(instrument_key).partition("|")
            try:
                exchange = Exchange(exchange_value)
            except ValueError:
                continue
            quotes.append(
                Quote(
                    tradingsymbol=tradingsymbol,
                    exchange=exchange,
                    last_price=float(ltpc["ltp"]),
                    timestamp=None,
                )
            )
        return quotes
