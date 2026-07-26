"""The broker abstraction (Adapter Pattern).

`BrokerInterface` is the contract every broker adapter must satisfy — the
rest of the application (services, scheduler, websocket layer) depends
only on this interface, never on a concrete `AngelOneAdapter` /
`UpstoxAdapter` / `ZerodhaAdapter`, so brokers can be swapped or added
without touching a single call site.

`BaseBrokerAdapter` implements everything that is identical across
brokers — HTTP client lifecycle, retry-on-transient-failure, the
detect-expired-token-then-refresh-then-retry-once flow, and the
reconnecting WebSocket loop — so concrete adapters only ever implement
broker-specific request/response shapes. This is what "never duplicate
broker logic" means in practice: shared behavior lives here exactly once.
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any

import httpx
import structlog

from app.brokers.ws_client import (
    ConnectHandler,
    HeadersFactory,
    MessageHandler,
    ReconnectingWebSocketClient,
    UrlFactory,
)
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
from app.domain.enums.trading import BrokerName, Exchange, HistoricalInterval, OrderVariety
from app.domain.exceptions.broker import (
    BrokerAPIError,
    BrokerAuthenticationError,
    BrokerConnectionError,
    TokenExpiredError,
)
from app.utils.retry import build_retrying

logger = structlog.get_logger(__name__)

#: Invoked once per incoming market-data tick after an adapter has
#: decoded the broker's raw WebSocket frame into a domain `Quote`.
TickHandler = Callable[[Quote], Awaitable[None]]


class BrokerInterface(ABC):
    """Uniform contract every broker adapter must implement.

    Every method is `async` because every implementation is I/O-bound
    (HTTP or WebSocket); a synchronous adapter would block the event loop
    the rest of the application relies on.
    """

    @abstractmethod
    async def login(self) -> TokenBundle:
        """Authenticate from stored credentials and return fresh tokens."""

    @abstractmethod
    async def refresh_token(self) -> TokenBundle:
        """Exchange the current refresh token for a new access token."""

    @abstractmethod
    async def get_profile(self) -> BrokerProfile:
        """Fetch the logged-in account's profile/entitlements."""

    @abstractmethod
    async def get_funds(self) -> BrokerFunds:
        """Fetch available margin/cash for the trading account."""

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Fetch open intraday/carry-forward positions."""

    @abstractmethod
    async def get_holdings(self) -> list[Holding]:
        """Fetch settled (T+1) demat holdings."""

    @abstractmethod
    async def get_orders(self) -> list[OrderDetail]:
        """Fetch today's order book."""

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """Place a new order."""

    @abstractmethod
    async def modify_order(self, order_id: str, order: OrderRequest) -> OrderResponse:
        """Modify an existing, still-open order."""

    @abstractmethod
    async def cancel_order(
        self, order_id: str, variety: OrderVariety = OrderVariety.REGULAR
    ) -> OrderResponse:
        """Cancel an existing, still-open order."""

    @abstractmethod
    async def ltp(self, exchange: Exchange, tradingsymbol: str) -> Quote:
        """Fetch the last-traded-price snapshot for one instrument."""

    @abstractmethod
    async def historical_data(
        self,
        exchange: Exchange,
        tradingsymbol: str,
        interval: HistoricalInterval,
        from_date: datetime,
        to_date: datetime,
    ) -> list[HistoricalBar]:
        """Fetch historical OHLCV candles for one instrument."""

    @abstractmethod
    async def start_websocket(
        self, on_tick: TickHandler, instruments: Sequence[tuple[Exchange, str]]
    ) -> None:
        """Open the market-data stream and subscribe to `instruments`."""

    @abstractmethod
    async def stop_websocket(self) -> None:
        """Close the market-data stream, if open."""

    @abstractmethod
    async def close(self) -> None:
        """Release this adapter's resources (HTTP client, WebSocket
        stream). Call once on application shutdown for every adapter
        instance obtained from `app.brokers.factory.get_broker_adapter`.
        """


class BaseBrokerAdapter(BrokerInterface, ABC):
    """Shared infrastructure for all concrete broker adapters.

    Subclasses must implement the `BrokerInterface` methods plus the two
    hooks declared here (`_auth_headers`, `_is_token_expired`); everything
    else — retries, error mapping, and WebSocket reconnection — is
    inherited unchanged.
    """

    def __init__(
        self,
        *,
        broker_name: BrokerName,
        base_url: str,
        request_timeout_seconds: float,
        max_retries: int,
        retry_backoff_seconds: float,
        retry_max_backoff_seconds: float,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._broker_name = broker_name
        self._client = http_client or httpx.AsyncClient(
            base_url=base_url, timeout=request_timeout_seconds
        )
        self._max_attempts = max_retries + 1
        self._retry_backoff_seconds = retry_backoff_seconds
        self._retry_max_backoff_seconds = retry_max_backoff_seconds
        self._access_token: str | None = None
        self._refresh_token_value: str | None = None
        self._ws_client: ReconnectingWebSocketClient | None = None

    @property
    def broker_name(self) -> BrokerName:
        """Which broker this adapter instance talks to."""

        return self._broker_name

    async def close(self) -> None:
        """Release the HTTP client and stop any open WebSocket stream.

        Call this on application shutdown for every adapter instance.
        """

        await self.stop_websocket()
        await self._client.aclose()

    # -- Shared request pipeline -------------------------------------------------

    @abstractmethod
    def _auth_headers(self) -> dict[str, str]:
        """Build the headers this broker expects for authenticated calls.

        Differs per broker (bearer token, `X-Api-Key` + token, etc.), so
        each adapter supplies its own; the retry/refresh flow that uses
        them is identical for all three.
        """

    @abstractmethod
    def _is_token_expired(self, response: httpx.Response) -> bool:
        """Detect this broker's specific "token expired/invalid" signal.

        Every broker reports this differently (a bare 401, a 403 with a
        specific error code, a 200 with an error field in the body), so
        the detection is broker-specific even though the resulting
        refresh-and-retry-once behavior in `_request` is not.
        """

    async def _request(
        self,
        method: str,
        url: str,
        *,
        authenticated: bool = True,
        retry_on_token_expiry: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send an HTTP request with retry-on-transient-failure and a
        single automatic refresh-then-retry on token expiry.

        This is the only place any adapter talks to `httpx` directly;
        every one of the 14 interface methods is implemented in terms of
        this method so retry/error-handling behavior can never drift
        between adapters.
        """

        retrying = build_retrying(
            max_attempts=self._max_attempts,
            initial_backoff_seconds=self._retry_backoff_seconds,
            max_backoff_seconds=self._retry_max_backoff_seconds,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    return await self._send_once(method, url, authenticated=authenticated, **kwargs)
        except TokenExpiredError:
            if not retry_on_token_expiry:
                raise
            logger.info("broker_token_expired_refreshing", broker=self._broker_name.value, url=url)
            await self.refresh_token()
            return await self._send_once(method, url, authenticated=authenticated, **kwargs)

        raise AssertionError("unreachable: tenacity always returns or raises")  # pragma: no cover

    async def _send_once(
        self, method: str, url: str, *, authenticated: bool, **kwargs: Any
    ) -> dict[str, Any]:
        """Perform exactly one HTTP call and classify the outcome.

        Raises:
            BrokerAuthenticationError: `authenticated=True` and `login()`
                has never populated `self._access_token` for this adapter
                instance.
            BrokerConnectionError: network failure or 5xx (retryable).
            TokenExpiredError: broker-specific expired-token signal.
            BrokerAPIError: any other 4xx (not retryable).
        """

        headers: dict[str, str] = dict(kwargs.pop("headers", None) or {})
        if authenticated:
            if not self._access_token:
                # Every adapter's `_auth_headers()` interpolates
                # `self._access_token` into an `Authorization` header
                # (e.g. `f"Bearer {self._access_token or ''}"`); with no
                # token that produces a value with trailing whitespace
                # (`"Bearer "`), which httpx's HTTP/1.1 layer rejects
                # outright as an illegal header value — a confusing
                # transport-layer crash instead of the actionable "log in
                # first" error this is. Caught here, before ever building
                # that header, so every adapter gets this for free.
                raise BrokerAuthenticationError(
                    f"{self._broker_name.value}: not authenticated — call login() first",
                    broker=self._broker_name,
                )
            headers.update(self._auth_headers())

        try:
            response = await self._client.request(method, url, headers=headers, **kwargs)
        except httpx.TransportError as exc:
            raise BrokerConnectionError(
                f"{self._broker_name.value}: network error calling {url}: {exc}",
                broker=self._broker_name,
            ) from exc

        if response.status_code >= 500:
            raise BrokerConnectionError(
                f"{self._broker_name.value}: server error {response.status_code} from {url}",
                broker=self._broker_name,
            )

        if self._is_token_expired(response):
            raise TokenExpiredError(
                f"{self._broker_name.value}: access token expired or invalid",
                broker=self._broker_name,
            )

        if response.status_code >= 400:
            raise BrokerAPIError(
                f"{self._broker_name.value}: request to {url} failed with {response.status_code}: {response.text}",
                broker=self._broker_name,
                status_code=response.status_code,
            )

        try:
            body = response.json()
        except ValueError as exc:
            # A 2xx/3xx status with an undecodable body (e.g. an HTML error
            # page from an intermediary proxy/gateway) is a broker-API
            # failure, not a transport failure — it must not escape as a
            # raw `json.JSONDecodeError`, which no caller written against
            # this adapter's documented `except BrokerError` contract would
            # catch.
            raise BrokerAPIError(
                f"{self._broker_name.value}: expected a JSON body from {url}, got unparseable content",
                broker=self._broker_name,
                status_code=response.status_code,
            ) from exc
        if not isinstance(body, dict):
            raise BrokerAPIError(
                f"{self._broker_name.value}: expected a JSON object from {url}, got {type(body).__name__}",
                broker=self._broker_name,
                status_code=response.status_code,
            )
        return body

    # -- Shared WebSocket lifecycle ----------------------------------------------

    async def _start_ws(
        self,
        url_factory: UrlFactory,
        on_message: MessageHandler,
        on_connect: ConnectHandler | None = None,
        headers_factory: HeadersFactory | None = None,
    ) -> None:
        """Create (if needed) and start the reconnecting WebSocket client.

        Adapters call this from their `start_websocket` implementation,
        supplying only the broker-specific URL/subscribe/parse pieces.
        `headers_factory` is optional and only needed by brokers whose
        WebSocket handshake requires extra headers (e.g. Angel One's
        bearer/feed-token headers); brokers that don't need it (Zerodha,
        Upstox) simply omit it and behavior is unchanged.
        """

        self._ws_client = ReconnectingWebSocketClient(
            url_factory=url_factory,
            on_message=on_message,
            on_connect=on_connect,
            headers_factory=headers_factory,
        )
        await self._ws_client.start()

    async def stop_websocket(self) -> None:
        """Stop the WebSocket stream, if one is running.

        Identical for every broker, so it is implemented once here rather
        than in each adapter.
        """

        if self._ws_client is not None:
            await self._ws_client.stop()
