"""Angel One SmartAPI broker adapter.

Implements `BrokerInterface` against Angel One SmartAPI's REST API
(`https://apiconnect.angelbroking.com`) and its "WebSocket 2.0" market-data
stream (`wss://smartapisocket.angelone.in/smart-stream`). All shared
plumbing — HTTP retry, 5xx/network error classification, token-expiry-
then-refresh-then-retry, and the reconnecting WebSocket loop — is inherited
unchanged from `BaseBrokerAdapter` / `app.brokers.ws_client`; this module
only shapes SmartAPI-specific requests and responses.

Several structural quirks of SmartAPI shape this file:

* Unlike Zerodha/Upstox, SmartAPI supports genuine password-based
  programmatic login: `login()` combines the account password with a
  TOTP generated from `credentials.totp_secret` (via `pyotp`), so no
  browser redirect is required. `refresh_token()` is also fully
  programmatic (SmartAPI issues a real `refreshToken`).
* SmartAPI signals an expired/invalid JWT in an unusual way: sometimes a
  bare HTTP 401/403, but often HTTP 200 with a JSON body carrying
  `"status": false` and an `"errorcode"` in the `"AG8xxx"` family. Because
  a 200 can still mean "token expired", `_is_token_expired` must inspect
  the parsed JSON body even for 200 responses — see that method's
  docstring for exactly how this interacts with `BaseBrokerAdapter._send_once`.
* Order placement, LTP snapshots, and historical candles are all keyed by
  a numeric `symboltoken` from Angel One's separate instrument-master JSON
  dump (refreshed daily), not by a plain trading symbol. Every method that
  needs one resolves it via `AngelOneInstrumentMaster`
  (`app.brokers.angel_one_instruments`), injectable through the
  `instrument_resolver` constructor argument (tests inject a trivial fake;
  production uses the real, disk-cached instrument-master lookup by
  default). Callers still pass a plain trading symbol (e.g. `"SBIN-EQ"` or
  the bare `"SBIN"`) — resolution to `symboltoken` happens inside this
  adapter, not the caller's responsibility.
"""

import dataclasses
import json
import struct
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import pyotp
import structlog
from websockets.asyncio.client import ClientConnection

from app.brokers.angel_one_instruments import AngelOneInstrumentMaster, SymbolResolver
from app.brokers.base import BaseBrokerAdapter, TickHandler
from app.config.settings import AngelOneCredentials, BrokerSettings
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
from app.core.logging import TRADE_LOGGER_NAME

logger = structlog.get_logger(__name__)
trade_logger = structlog.get_logger(TRADE_LOGGER_NAME)

#: HTTP statuses SmartAPI sometimes (but not always — see `_is_token_expired`)
#: uses for an expired/invalid access token.
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403

#: Prefix shared by SmartAPI's authentication/JWT error codes (e.g.
#: `"AG8001"` for an expired token, `"AG8002"` for an invalid token).
_AUTH_ERROR_CODE_PREFIX = "AG8"

#: Angel One product codes <-> canonical `ProductType`. Several SmartAPI
#: aliases map to the same canonical product (e.g. `INTRADAY`/`MIS` both
#: mean intraday); the reverse mapping therefore picks one canonical
#: SmartAPI spelling per `ProductType` rather than being a naive inverse.
_PRODUCT_FROM_ANGEL_ONE: dict[str, ProductType] = {
    "DELIVERY": ProductType.DELIVERY,
    # "CNC" ("Cash and Carry") is the product code SmartAPI's own
    # `getProfile` response actually uses for delivery/equity-cash trades
    # on a live account — confirmed by live testing; `DELIVERY` above is
    # kept too since it's SmartAPI's documented order-placement code for
    # the same product.
    "CNC": ProductType.DELIVERY,
    "INTRADAY": ProductType.INTRADAY,
    "MIS": ProductType.INTRADAY,
    "CARRYFORWARD": ProductType.MARGIN,
    "NRML": ProductType.MARGIN,
    "MARGIN": ProductType.MARGIN,
    "BO": ProductType.BRACKET_ORDER,
    "CO": ProductType.COVER_ORDER,
}
_PRODUCT_TO_ANGEL_ONE: dict[ProductType, str] = {
    ProductType.DELIVERY: "DELIVERY",
    ProductType.INTRADAY: "INTRADAY",
    ProductType.MARGIN: "CARRYFORWARD",
    ProductType.BRACKET_ORDER: "BO",
    ProductType.COVER_ORDER: "CO",
}

#: Canonical `OrderVariety` <-> SmartAPI's `variety` string. SmartAPI has no
#: direct analog for `ICEBERG`; `ROBO` (its bracket-order variety) is used
#: as the closest documented best-effort mapping.
_VARIETY_TO_ANGEL_ONE: dict[OrderVariety, str] = {
    OrderVariety.REGULAR: "NORMAL",
    OrderVariety.AFTER_MARKET: "AMO",
    OrderVariety.STOP_LOSS: "STOPLOSS",
    OrderVariety.ICEBERG: "ROBO",
}

#: Canonical `OrderType` <-> SmartAPI's `ordertype` string.
_ORDER_TYPE_TO_ANGEL_ONE: dict[OrderType, str] = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.STOP_LOSS: "STOPLOSS_LIMIT",
    OrderType.STOP_LOSS_MARKET: "STOPLOSS_MARKET",
}
_ORDER_TYPE_FROM_ANGEL_ONE: dict[str, OrderType] = {
    v: k for k, v in _ORDER_TYPE_TO_ANGEL_ONE.items()
}

#: Canonical `OrderValidity` <-> SmartAPI's `duration` string. SmartAPI has
#: no good-till-cancelled order type, so `GOOD_TILL_CANCELLED` is
#: deliberately absent — order calls raise `BrokerAPIError` for it.
_VALIDITY_TO_ANGEL_ONE: dict[OrderValidity, str] = {
    OrderValidity.DAY: "DAY",
    OrderValidity.IMMEDIATE_OR_CANCEL: "IOC",
}

#: SmartAPI order-book `status` strings (lower-cased before lookup) <->
#: canonical `OrderStatus`. Any value not present here maps to
#: `OrderStatus.UNKNOWN` rather than raising.
_ORDER_STATUS_FROM_ANGEL_ONE: dict[str, OrderStatus] = {
    "complete": OrderStatus.COMPLETE,
    "open": OrderStatus.OPEN,
    "open pending": OrderStatus.OPEN,
    "cancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
    "trigger pending": OrderStatus.TRIGGER_PENDING,
}

#: Canonical `HistoricalInterval` <-> SmartAPI's `interval` string.
_INTERVAL_TO_ANGEL_ONE: dict[HistoricalInterval, str] = {
    HistoricalInterval.ONE_MINUTE: "ONE_MINUTE",
    HistoricalInterval.THREE_MINUTE: "THREE_MINUTE",
    HistoricalInterval.FIVE_MINUTE: "FIVE_MINUTE",
    HistoricalInterval.TEN_MINUTE: "TEN_MINUTE",
    HistoricalInterval.FIFTEEN_MINUTE: "FIFTEEN_MINUTE",
    HistoricalInterval.THIRTY_MINUTE: "THIRTY_MINUTE",
    HistoricalInterval.ONE_HOUR: "ONE_HOUR",
    HistoricalInterval.ONE_DAY: "ONE_DAY",
}

#: SmartAPI's fixed WebSocket 2.0 market-data stream URL (no query params
#: or path variables; auth is carried entirely in connection headers).
_WS_URL = "wss://smartapisocket.angelone.in/smart-stream"

#: Canonical `Exchange` <-> SmartAPI's WebSocket `exchangeType` integer.
#: Segments with no direct SmartAPI feed mapping documented here
#: (BFO/MCX/CDS) default to `1` (NSE cash) as a reasonable best-effort
#: fallback rather than raising, since a wrong tick decode is preferable
#: to a hard failure in a market-data subscribe call.
_EXCHANGE_TYPE_TO_ANGEL_ONE: dict[Exchange, int] = {
    Exchange.NSE: 1,
    Exchange.NFO: 2,
    Exchange.BSE: 3,
}
_DEFAULT_EXCHANGE_TYPE = 1
_EXCHANGE_TYPE_FROM_ANGEL_ONE: dict[int, Exchange] = {
    v: k for k, v in _EXCHANGE_TYPE_TO_ANGEL_ONE.items()
}

#: SmartAPI's `getProfile` response lists enabled segments as lowercase,
#: cash/derivatives-distinct codes (confirmed by live testing:
#: `"nse_cm"`, `"nse_fo"`, `"bse_cm"`, `"bse_fo"`, `"cde_fo"`, `"mcx_fo"`,
#: `"ncx_fo"`) — a completely different vocabulary from the plain
#: `"NSE"`/`"BSE"` this codebase's `Exchange` enum uses everywhere else
#: (order placement, historical data, WebSocket). `Exchange(raw_exchange)`
#: therefore never matches any real profile response; this table is
#: `getProfile`'s own translation. `"ncx_fo"` (NCDEX) has no corresponding
#: `Exchange` member and is intentionally left unmapped — it falls
#: through to `get_profile`'s existing "unknown, skip" branch.
_EXCHANGE_FROM_ANGEL_ONE_PROFILE: dict[str, Exchange] = {
    "nse_cm": Exchange.NSE,
    "nse_fo": Exchange.NFO,
    "bse_cm": Exchange.BSE,
    "bse_fo": Exchange.BFO,
    "cde_fo": Exchange.CDS,
    "mcx_fo": Exchange.MCX,
}

#: Byte offsets/widths of SmartAPI's LTP-mode (mode 1) binary tick packet,
#: confirmed against a real live frame (51 bytes total): token is a
#: 25-byte field (offset 2-27), not the 24 bytes this module previously
#: guessed to make the arithmetic "tile" to a 50-byte total — that guess
#: was wrong. With these offsets, a live frame's decoded LTP matched the
#: same instrument's `ltp()` REST value exactly (1015.0) and its decoded
#: timestamp fell in the expected epoch-millis range; with the old
#: (24-byte) offsets, every real frame decoded a nonsensical timestamp
#: that crashed `datetime.fromtimestamp` with `OSError: [Errno 22]` on
#: Windows (out-of-range value), which surfaced as a permanent
#: connect-crash-reconnect loop in `start_websocket` — see
#: `_parse_tick`'s docstring.
_TICK_EXCHANGE_TYPE_OFFSET = 1
_TICK_TOKEN_OFFSET = 2
_TICK_TOKEN_LENGTH = 25
_TICK_TIMESTAMP_OFFSET = 35
_TICK_LTP_OFFSET = 43
_TICK_MIN_LENGTH = 51
_PAISE_PER_RUPEE = 100.0


class AngelOneAdapter(BaseBrokerAdapter):
    """`BrokerInterface` implementation for Angel One SmartAPI.

    Only broker-specific request/response shaping lives here; retries,
    5xx/network error classification, and the token-expiry-refresh-retry
    flow are all inherited from `BaseBrokerAdapter`.
    """

    def __init__(
        self,
        credentials: AngelOneCredentials,
        broker_settings: BrokerSettings,
        http_client: httpx.AsyncClient | None = None,
        instrument_resolver: SymbolResolver | None = None,
    ) -> None:
        super().__init__(
            broker_name=BrokerName.ANGEL_ONE,
            base_url=credentials.base_url,
            request_timeout_seconds=broker_settings.request_timeout_seconds,
            max_retries=broker_settings.max_retries,
            retry_backoff_seconds=broker_settings.retry_backoff_seconds,
            retry_max_backoff_seconds=broker_settings.retry_max_backoff_seconds,
            http_client=http_client,
        )
        self._credentials = credentials
        self._access_token: str | None = None
        self._refresh_token_value: str | None = None
        self._feed_token: str | None = None
        #: Defaults to a real, disk-cached `AngelOneInstrumentMaster` — a
        #: separate HTTP client of its own, since the scrip master lives
        #: on a different host than `credentials.base_url` and needs no
        #: auth. Tests inject a trivial fake instead (see `SymbolResolver`).
        self._instruments: SymbolResolver = instrument_resolver or AngelOneInstrumentMaster()

    @property
    def instrument_master(self) -> SymbolResolver:
        """The instrument resolver this adapter was constructed with.

        Exposed so other infrastructure that needs Angel One's raw
        scrip-master rows (e.g. `app.options.option_chain_service
        .AngelOneOptionInstrumentSource`) can reuse this adapter's
        existing resolver/cache instead of constructing a second one.
        Typed as `SymbolResolver` (not the concrete
        `AngelOneInstrumentMaster`) because tests may inject a trivial
        fake here too — callers needing the concrete type's extra
        methods (e.g. `.rows()`) should `isinstance`-check first.
        """

        return self._instruments

    async def _resolve_token(self, exchange: Exchange, tradingsymbol: str) -> str:
        """Resolve `tradingsymbol` to Angel One's numeric `symboltoken`.

        The one place every method that talks to SmartAPI's REST/WebSocket
        APIs goes through, so the resolver is never called with slightly
        different arguments from different call sites.
        """

        return await self._instruments.resolve(exchange, tradingsymbol)

    async def close(self) -> None:
        """Release the HTTP client, WebSocket stream, and the instrument
        resolver's own HTTP client (if it owns one — see
        `AngelOneInstrumentMaster.close`).
        """

        await super().close()
        instruments = self._instruments
        if isinstance(instruments, AngelOneInstrumentMaster):
            await instruments.close()

    # -- Auth hooks required by BaseBrokerAdapter ---------------------------------

    def _common_headers(self) -> dict[str, str]:
        """Headers SmartAPI requires on *every* request, even unauthenticated login.

        `X-ClientLocalIP`/`X-ClientPublicIP`/`X-MACAddress` are placeholder
        loopback/broadcast values: SmartAPI's contract requires these
        headers to be present and non-empty, but accepts placeholders for
        non-browser server-to-server integrations where no real client
        network identity exists.
        """

        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": self._credentials.api_key,
        }

    def _auth_headers(self) -> dict[str, str]:
        """SmartAPI's authenticated-request headers: common headers plus a bearer JWT."""

        headers = self._common_headers()
        headers["Authorization"] = f"Bearer {self._access_token or ''}"
        return headers

    def _is_token_expired(self, response: httpx.Response) -> bool:
        """Detect SmartAPI's expired/invalid-JWT signal.

        `BaseBrokerAdapter._send_once` calls this hook after the 5xx check
        but *before* its own generic `status_code >= 400` handling — so
        this method is reached for every 2xx/3xx/4xx response, including a
        plain HTTP 200. That matters here because SmartAPI reports an
        expired/invalid token in two different ways: sometimes a bare 401
        or 403, but often HTTP 200 with a JSON body carrying
        `"status": false` and an `"errorcode"` in the `"AG8xxx"` family
        (e.g. `"AG8001"`). Both signals are therefore checked; a
        non-JSON or unparseable 200 body can never be mistaken for an
        expiry signal.
        """

        if response.status_code in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
            return True
        if response.status_code != 200:
            return False
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - any undecodable 200 body is simply "not expired"
            return False
        if not isinstance(body, dict) or body.get("status") is not False:
            return False
        return str(body.get("errorcode", "")).startswith(_AUTH_ERROR_CODE_PREFIX)

    def _store_tokens(self, body: dict[str, Any]) -> TokenBundle:
        """Validate a login/refresh response and persist its token triple.

        Shared by `login()` and `refresh_token()` since SmartAPI returns
        the identical `{"status", "data": {"jwtToken", "refreshToken",
        "feedToken"}}` shape from both endpoints.
        """

        if not body.get("status") or "data" not in body:
            message = body.get("message") or "authentication failed"
            raise BrokerAuthenticationError(
                f"{self._broker_name.value}: {message}", broker=self._broker_name
            )

        data = body["data"]
        self._access_token = data["jwtToken"]
        self._refresh_token_value = data["refreshToken"]
        self._feed_token = data.get("feedToken")
        return TokenBundle(
            access_token=self._access_token,
            refresh_token=self._refresh_token_value,
            feed_token=self._feed_token,
            expires_at=None,
        )

    # -- Auth -----------------------------------------------------------------

    async def login(self) -> TokenBundle:
        """Authenticate with client code, password, and a freshly generated TOTP.

        Unlike Zerodha/Upstox, SmartAPI supports genuine password-based
        programmatic login: no browser redirect is required, only the
        base32 TOTP secret configured out-of-band with Angel One.
        """

        totp_code = pyotp.TOTP(self._credentials.totp_secret).now()
        body = await self._request(
            "POST",
            "/rest/auth/angelbroking/user/v1/loginByPassword",
            authenticated=False,
            retry_on_token_expiry=False,
            headers=self._common_headers(),
            json={
                "clientcode": self._credentials.client_code,
                "password": self._credentials.password,
                "totp": totp_code,
            },
        )
        return self._store_tokens(body)

    async def refresh_token(self) -> TokenBundle:
        """Exchange the stored `refreshToken` for a new JWT/refresh/feed token triple.

        Unlike Zerodha/Upstox, SmartAPI issues a real refresh token, so
        this is fully programmatic — but only once `login()` has run at
        least once in this process (or the refresh token was otherwise
        restored) to populate `self._refresh_token_value`.
        """

        if self._refresh_token_value is None:
            raise BrokerAuthenticationError(
                "angel_one: no refresh token available for this session; call login() first",
                broker=self._broker_name,
            )

        body = await self._request(
            "POST",
            "/rest/auth/angelbroking/jwt/v1/generateTokens",
            authenticated=False,
            retry_on_token_expiry=False,
            headers=self._common_headers(),
            json={"refreshToken": self._refresh_token_value},
        )
        return self._store_tokens(body)

    # -- Account/profile --------------------------------------------------------

    async def get_profile(self) -> BrokerProfile:
        """Fetch the logged-in account's profile and entitlements."""

        body = await self._request("GET", "/rest/secure/angelbroking/user/v1/getProfile")
        data = body["data"]

        exchanges: list[Exchange] = []
        for raw_exchange in data.get("exchanges", []):
            exchange = _EXCHANGE_FROM_ANGEL_ONE_PROFILE.get(str(raw_exchange).lower())
            if exchange is not None:
                exchanges.append(exchange)
            else:
                logger.debug("angel_one_unknown_exchange", value=raw_exchange)

        products: list[ProductType] = []
        for raw_product in data.get("products", []):
            product = _PRODUCT_FROM_ANGEL_ONE.get(str(raw_product).upper())
            if product is not None:
                products.append(product)
            else:
                logger.debug("angel_one_unknown_product", value=raw_product)

        return BrokerProfile(
            broker=BrokerName.ANGEL_ONE,
            client_id=data["clientcode"],
            display_name=data["name"],
            email=data.get("email"),
            exchanges_enabled=tuple(exchanges),
            products_enabled=tuple(products),
        )

    async def get_funds(self) -> BrokerFunds:
        """Fetch available margin/cash via SmartAPI's RMS (risk management system) endpoint."""

        body = await self._request("GET", "/rest/secure/angelbroking/user/v1/getRMS")
        data = body["data"]
        return BrokerFunds(
            broker=BrokerName.ANGEL_ONE,
            available_cash=float(data["availablecash"]),
            used_margin=float(data["utiliseddebits"]),
            total_balance=float(data["net"]),
            raw=data,
        )

    async def get_positions(self) -> list[Position]:
        """Fetch open intraday/carry-forward positions."""

        body = await self._request("GET", "/rest/secure/angelbroking/order/v1/getPosition")
        positions: list[Position] = []
        for item in body.get("data") or []:
            product = _PRODUCT_FROM_ANGEL_ONE.get(str(item["producttype"]).upper())
            if product is None:
                logger.debug("angel_one_unknown_product", value=item["producttype"])
                continue
            positions.append(
                Position(
                    tradingsymbol=item["tradingsymbol"],
                    exchange=Exchange(item["exchange"]),
                    product=product,
                    quantity=int(item["netqty"]),
                    average_price=float(item["avgnetprice"]),
                    last_price=float(item["ltp"]),
                    pnl=self._pnl(
                        item, quantity=int(item["netqty"]), average_price=float(item["avgnetprice"])
                    ),
                )
            )
        return positions

    async def get_holdings(self) -> list[Holding]:
        """Fetch settled (T+1) demat holdings."""

        body = await self._request("GET", "/rest/secure/angelbroking/portfolio/v1/getHolding")
        holdings: list[Holding] = []
        for item in body.get("data") or []:
            holdings.append(
                Holding(
                    tradingsymbol=item["tradingsymbol"],
                    exchange=Exchange(item["exchange"]),
                    isin=item["isin"],
                    quantity=int(item["quantity"]),
                    average_price=float(item["averageprice"]),
                    last_price=float(item["ltp"]),
                    pnl=self._pnl(
                        item,
                        quantity=int(item["quantity"]),
                        average_price=float(item["averageprice"]),
                    ),
                )
            )
        return holdings

    @staticmethod
    def _pnl(item: dict[str, Any], *, quantity: int, average_price: float) -> float:
        """Prefer SmartAPI's own `pnl` figure if present; otherwise compute it.

        SmartAPI's position/holding payloads don't consistently document a
        `pnl` field across API versions, so this defensively uses one if
        the broker supplied it and falls back to
        `(last_price - average_price) * quantity` otherwise.
        """

        if "pnl" in item:
            return float(item["pnl"])
        return (float(item["ltp"]) - average_price) * quantity

    async def get_orders(self) -> list[OrderDetail]:
        """Fetch today's order book."""

        body = await self._request("GET", "/rest/secure/angelbroking/order/v1/getOrderBook")
        orders: list[OrderDetail] = []
        for item in body.get("data") or []:
            product = _PRODUCT_FROM_ANGEL_ONE.get(str(item["producttype"]).upper())
            if product is None:
                logger.debug("angel_one_unknown_product", value=item["producttype"])
                continue
            orders.append(
                OrderDetail(
                    order_id=item["orderid"],
                    tradingsymbol=item["tradingsymbol"],
                    exchange=Exchange(item["exchange"]),
                    status=_ORDER_STATUS_FROM_ANGEL_ONE.get(
                        str(item["status"]).lower(), OrderStatus.UNKNOWN
                    ),
                    transaction_type=OrderSide(item["transactiontype"]),
                    order_type=_ORDER_TYPE_FROM_ANGEL_ONE.get(
                        str(item["ordertype"]).upper(), OrderType.MARKET
                    ),
                    product=product,
                    quantity=int(item["quantity"]),
                    filled_quantity=int(item["filledshares"]),
                    pending_quantity=int(item["unfilledshares"]),
                    price=float(item["price"]),
                    average_price=float(item["averageprice"]),
                    order_timestamp=self._parse_order_timestamp(item.get("updatetime")),
                )
            )
        return orders

    @staticmethod
    def _parse_order_timestamp(value: str | None) -> datetime | None:
        """Parse SmartAPI's `"DD-MMM-YYYY HH:MM:SS"` order-book timestamp, tolerating `None`/bad values."""

        if not value:
            return None
        try:
            return datetime.strptime(value, "%d-%b-%Y %H:%M:%S")
        except ValueError:
            return None

    # -- Orders -----------------------------------------------------------------

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """Place a new order via `POST /rest/secure/angelbroking/order/v1/placeOrder`.

        `symboltoken` is resolved from `order.tradingsymbol` via the
        instrument resolver — see the module docstring.
        """

        symboltoken = await self._resolve_token(order.exchange, order.tradingsymbol)
        payload = self._place_order_payload(order, symboltoken)
        trade_logger.info(
            "order_placement_requested",
            broker=BrokerName.ANGEL_ONE.value,
            side=order.transaction_type.value,
            symbol=order.tradingsymbol,
            exchange=order.exchange.value,
            quantity=order.quantity,
            order_type=order.order_type.value,
            product=order.product.value,
            price=order.price,
        )
        try:
            body = await self._request(
                "POST", "/rest/secure/angelbroking/order/v1/placeOrder", json=payload
            )
        except BrokerAPIError as exc:
            trade_logger.warning(
                "order_placement_failed",
                broker=BrokerName.ANGEL_ONE.value,
                side=order.transaction_type.value,
                symbol=order.tradingsymbol,
                exception=repr(exc),
            )
            raise self._as_order_rejection(exc) from exc
        if not body.get("status"):
            trade_logger.warning(
                "order_placement_rejected",
                broker=BrokerName.ANGEL_ONE.value,
                side=order.transaction_type.value,
                symbol=order.tradingsymbol,
                response=body,
            )
            raise self._rejection_from_body(body)
        data = body["data"]
        trade_logger.info(
            "order_placed",
            broker=BrokerName.ANGEL_ONE.value,
            side=order.transaction_type.value,
            symbol=order.tradingsymbol,
            order_id=data["orderid"],
        )
        return OrderResponse(order_id=data["orderid"], broker=BrokerName.ANGEL_ONE, raw=data)

    async def modify_order(self, order_id: str, order: OrderRequest) -> OrderResponse:
        """Modify an existing, still-open order via `POST /rest/secure/angelbroking/order/v1/modifyOrder`."""

        symboltoken = await self._resolve_token(order.exchange, order.tradingsymbol)
        payload = {
            "orderid": order_id,
            "variety": _VARIETY_TO_ANGEL_ONE[order.variety],
            "tradingsymbol": order.tradingsymbol,
            "symboltoken": symboltoken,
            "exchange": order.exchange.value,
            "ordertype": _ORDER_TYPE_TO_ANGEL_ONE[order.order_type],
            "producttype": _PRODUCT_TO_ANGEL_ONE[order.product],
            "duration": self._validity_to_angel_one(order.validity),
            "price": str(order.price) if order.price is not None else "0",
            "quantity": str(order.quantity),
        }
        try:
            body = await self._request(
                "POST", "/rest/secure/angelbroking/order/v1/modifyOrder", json=payload
            )
        except BrokerAPIError as exc:
            raise self._as_order_rejection(exc) from exc
        if not body.get("status"):
            raise self._rejection_from_body(body)
        data = body["data"]
        return OrderResponse(order_id=data["orderid"], broker=BrokerName.ANGEL_ONE, raw=data)

    async def cancel_order(
        self, order_id: str, variety: OrderVariety = OrderVariety.REGULAR
    ) -> OrderResponse:
        """Cancel an existing, still-open order via `POST /rest/secure/angelbroking/order/v1/cancelOrder`."""

        payload = {"variety": _VARIETY_TO_ANGEL_ONE[variety], "orderid": order_id}
        try:
            body = await self._request(
                "POST", "/rest/secure/angelbroking/order/v1/cancelOrder", json=payload
            )
        except BrokerAPIError as exc:
            raise self._as_order_rejection(exc) from exc
        if not body.get("status"):
            raise self._rejection_from_body(body)
        data = body["data"]
        return OrderResponse(order_id=data["orderid"], broker=BrokerName.ANGEL_ONE, raw=data)

    def _place_order_payload(self, order: OrderRequest, symboltoken: str) -> dict[str, Any]:
        """Build the JSON body SmartAPI expects for `place_order`."""

        return {
            "variety": _VARIETY_TO_ANGEL_ONE[order.variety],
            "tradingsymbol": order.tradingsymbol,
            "symboltoken": symboltoken,
            "transactiontype": order.transaction_type.value,
            "exchange": order.exchange.value,
            "ordertype": _ORDER_TYPE_TO_ANGEL_ONE[order.order_type],
            "producttype": _PRODUCT_TO_ANGEL_ONE[order.product],
            "duration": self._validity_to_angel_one(order.validity),
            "price": str(order.price) if order.price is not None else "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(order.quantity),
        }

    @staticmethod
    def _validity_to_angel_one(validity: OrderValidity) -> str:
        """Map a canonical `OrderValidity` to SmartAPI's `duration` string.

        Raises `BrokerAPIError` for `GOOD_TILL_CANCELLED`, which SmartAPI
        does not support.
        """

        mapped = _VALIDITY_TO_ANGEL_ONE.get(validity)
        if mapped is None:
            raise BrokerAPIError(
                f"angel_one: validity {validity.value} is not supported",
                broker=BrokerName.ANGEL_ONE,
            )
        return mapped

    def _as_order_rejection(self, exc: BrokerAPIError) -> OrderRejectionError:
        """Re-raise a generic `BrokerAPIError` (a genuine 4xx) from an order
        call as an `OrderRejectionError`, preferring SmartAPI's `message`
        field if the response body was JSON.

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

    def _rejection_from_body(self, body: dict[str, Any]) -> OrderRejectionError:
        """Build an `OrderRejectionError` from a SmartAPI `"status": false`
        business-rejection body returned with HTTP 200 (the common case for
        order-placement failures like insufficient funds or an invalid
        instrument — as opposed to a token-expiry signal, which
        `_is_token_expired` intercepts before this is ever reached).
        """

        message = body.get("message") or "order rejected"
        error_code = body.get("errorcode")
        return OrderRejectionError(
            f"{self._broker_name.value}: order rejected: {message}",
            broker=self._broker_name,
            error_code=str(error_code) if error_code is not None else None,
        )

    # -- Market data --------------------------------------------------------------

    async def ltp(self, exchange: Exchange, tradingsymbol: str) -> Quote:
        """Fetch a last-traded-price snapshot via `POST /rest/secure/angelbroking/order/v1/getLtpData`.

        `symboltoken` is resolved from `tradingsymbol` — see the module
        docstring.
        """

        symboltoken = await self._resolve_token(exchange, tradingsymbol)
        payload = {
            "exchange": exchange.value,
            "tradingsymbol": tradingsymbol,
            "symboltoken": symboltoken,
        }
        body = await self._request(
            "POST", "/rest/secure/angelbroking/order/v1/getLtpData", json=payload
        )
        data = body["data"]
        return Quote(
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            last_price=float(data["ltp"]),
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
        """Fetch historical OHLCV candles via `POST /rest/secure/angelbroking/historical/v1/getCandleData`.

        `symboltoken` is resolved from `tradingsymbol` — see the module
        docstring.
        """

        symboltoken = await self._resolve_token(exchange, tradingsymbol)
        payload = {
            "exchange": exchange.value,
            "symboltoken": symboltoken,
            "interval": _INTERVAL_TO_ANGEL_ONE[interval],
            "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
            "todate": to_date.strftime("%Y-%m-%d %H:%M"),
        }
        body = await self._request(
            "POST", "/rest/secure/angelbroking/historical/v1/getCandleData", json=payload
        )
        candles = body.get("data") or []
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
        """Open SmartAPI's WebSocket 2.0 market-data stream and subscribe in LTP mode.

        SmartAPI 2.0 authenticates the WebSocket handshake entirely via
        connection headers (`Authorization`, `x-api-key`, `x-client-code`,
        `x-feed-token`) rather than the URL, so this passes a
        `headers_factory` through to `BaseBrokerAdapter._start_ws` (an
        additive extension of the shared `ReconnectingWebSocketClient` —
        see `app.brokers.ws_client`) so those headers are recomputed fresh
        on every (re)connect, picking up a rotated token automatically.

        Each `(Exchange, str)` pair in `instruments` is resolved to a
        numeric `symboltoken` before subscribing — see the module
        docstring. Incoming ticks report back the original `tradingsymbol`
        the caller subscribed with (not the raw token), via the
        `token -> tradingsymbol` map built from that same resolution.

        The binary tick-packet layout parsed in `_parse_tick` (byte 0
        subscription mode, byte 1 exchange type, bytes 2-27 a 25-byte
        token string, bytes 27-35 a little-endian int64 sequence number,
        bytes 35-43 a little-endian int64 exchange timestamp in epoch
        millis, bytes 43-51 a little-endian int64 last traded price in
        paise — 51 bytes total) is confirmed against a real live frame:
        its decoded LTP matched the same instrument's `ltp()` REST value
        exactly. See `_TICK_TOKEN_LENGTH`'s comment for how this differs
        from an earlier, incorrect 24-byte-token guess.
        """

        tokens_by_exchange_type: dict[int, list[str]] = {}
        symbol_by_token: dict[str, str] = {}
        for exchange, tradingsymbol in instruments:
            token = await self._resolve_token(exchange, tradingsymbol)
            exchange_type = _EXCHANGE_TYPE_TO_ANGEL_ONE.get(exchange, _DEFAULT_EXCHANGE_TYPE)
            tokens_by_exchange_type.setdefault(exchange_type, []).append(token)
            symbol_by_token[token] = tradingsymbol

        async def url_factory() -> str:
            return _WS_URL

        async def headers_factory() -> dict[str, str]:
            return {
                "Authorization": f"Bearer {self._access_token or ''}",
                "x-api-key": self._credentials.api_key,
                "x-client-code": self._credentials.client_code,
                "x-feed-token": self._feed_token or "",
            }

        async def on_connect(connection: ClientConnection) -> None:
            token_list = [
                {"exchangeType": exchange_type, "tokens": tokens}
                for exchange_type, tokens in tokens_by_exchange_type.items()
            ]
            subscribe_message = {
                "correlationID": "ws-sub",
                "action": 1,
                "params": {"mode": 1, "tokenList": token_list},
            }
            await connection.send(json.dumps(subscribe_message))

        async def on_message(message: str | bytes) -> None:
            if not isinstance(message, bytes):
                return  # SmartAPI's tick stream is binary-only; JSON frames are control/error acks.
            quote = self._parse_tick(message)
            if quote is None:
                return
            # `quote.tradingsymbol` is the raw token decoded from the wire
            # frame; report back the caller's original symbol wherever it's
            # recognized (always, for a token this call itself subscribed).
            resolved_symbol = symbol_by_token.get(quote.tradingsymbol, quote.tradingsymbol)
            if resolved_symbol != quote.tradingsymbol:
                quote = dataclasses.replace(quote, tradingsymbol=resolved_symbol)
            await on_tick(quote)

        await self._start_ws(url_factory, on_message, on_connect, headers_factory=headers_factory)

    @staticmethod
    def _parse_tick(frame: bytes) -> Quote | None:
        """Decode a SmartAPI LTP-mode (mode 1) binary tick packet.

        See `_TICK_TOKEN_LENGTH`'s comment and `start_websocket`'s
        docstring for how this layout was confirmed against live data.
        """

        if len(frame) < _TICK_MIN_LENGTH:
            return None

        exchange_type = frame[_TICK_EXCHANGE_TYPE_OFFSET]
        token_bytes = frame[_TICK_TOKEN_OFFSET : _TICK_TOKEN_OFFSET + _TICK_TOKEN_LENGTH]
        token = token_bytes.split(b"\x00", 1)[0].decode("ascii", errors="ignore")
        (timestamp_ms,) = struct.unpack_from("<q", frame, _TICK_TIMESTAMP_OFFSET)
        (ltp_paise,) = struct.unpack_from("<q", frame, _TICK_LTP_OFFSET)

        exchange = _EXCHANGE_TYPE_FROM_ANGEL_ONE.get(exchange_type, Exchange.NSE)
        timestamp = AngelOneAdapter._safe_epoch_millis_to_datetime(timestamp_ms)

        return Quote(
            tradingsymbol=token,
            exchange=exchange,
            last_price=ltp_paise / _PAISE_PER_RUPEE,
            timestamp=timestamp,
        )

    @staticmethod
    def _safe_epoch_millis_to_datetime(timestamp_ms: int) -> datetime | None:
        """Convert epoch millis to a `datetime`, tolerating a garbage value.

        A single unexpected/malformed frame (a future SmartAPI protocol
        change, a mode this adapter doesn't yet handle, or corrupted
        data) must not crash the whole tick stream: on Windows,
        `datetime.fromtimestamp` raises `OSError` (not just
        `OverflowError`/`ValueError`, which is what it raises on Linux)
        for a value the OS's C runtime rejects as out of range — and
        `ReconnectingWebSocketClient` treats any exception from
        `on_message` as "reconnect", so one bad frame would otherwise
        put the stream into a permanent connect-crash-reconnect loop
        instead of just dropping that one tick's timestamp.
        """

        if timestamp_ms <= 0:
            return None
        try:
            return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
