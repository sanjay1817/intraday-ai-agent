"""Generic reconnecting WebSocket client shared by every broker adapter.

Reconnect-with-backoff is the one piece of "broker logic" that is
identical across Angel One, Upstox, and Zerodha's streaming feeds even
though their URLs, auth handshakes, and wire formats differ completely.
Implementing it once here — and having each adapter inject only the
broker-specific pieces — is what keeps `start_websocket`/`stop_websocket`
from being duplicated three times.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import structlog
from websockets.asyncio.client import ClientConnection, connect

logger = structlog.get_logger(__name__)

#: Called with each raw incoming frame (adapters decode/parse it).
MessageHandler = Callable[[str | bytes], Awaitable[None]]

#: Called once per successful (re)connect, e.g. to send a subscribe frame.
ConnectHandler = Callable[[ClientConnection], Awaitable[None]]

#: Called to obtain the connection URL fresh on every (re)connect attempt,
#: so a rotated/expired feed token is picked up automatically.
UrlFactory = Callable[[], Awaitable[str]]

#: Called to obtain extra connection headers fresh on every (re)connect
#: attempt (e.g. Angel One's bearer/feed-token headers, which can rotate
#: independently of the URL). Optional: brokers whose handshake needs no
#: extra headers (Zerodha, Upstox) simply never pass this in, and the
#: connection is opened exactly as before.
HeadersFactory = Callable[[], Awaitable[Mapping[str, str] | None]]


class ReconnectingWebSocketClient:
    """Owns a single WebSocket connection with automatic reconnection.

    The connection loop runs as a background `asyncio.Task`; `start()`
    and `stop()` are idempotent so adapters can call them freely from
    `start_websocket()`/`stop_websocket()` without tracking state themselves.
    """

    def __init__(
        self,
        url_factory: UrlFactory,
        on_message: MessageHandler,
        on_connect: ConnectHandler | None = None,
        headers_factory: HeadersFactory | None = None,
        initial_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 30.0,
    ) -> None:
        self._url_factory = url_factory
        self._on_message = on_message
        self._on_connect = on_connect
        self._headers_factory = headers_factory
        self._initial_backoff_seconds = initial_backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    @property
    def is_running(self) -> bool:
        """Whether the background connection loop is currently active."""

        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the reconnecting connection loop, if not already running."""

        if self.is_running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the connection loop and wait for it to finish cleanly."""

        self._stop_event.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _run(self) -> None:
        """Connect, dispatch messages, and reconnect with exponential
        backoff on any failure, until `stop()` is called.
        """

        backoff = self._initial_backoff_seconds
        while not self._stop_event.is_set():
            try:
                url = await self._url_factory()
                connect_kwargs: dict[str, Any] = {}
                if self._headers_factory is not None:
                    headers = await self._headers_factory()
                    if headers is not None:
                        connect_kwargs["additional_headers"] = headers
                async with connect(url, **connect_kwargs) as connection:
                    backoff = self._initial_backoff_seconds
                    if self._on_connect is not None:
                        await self._on_connect(connection)
                    async for message in connection:
                        await self._on_message(message)
            except asyncio.CancelledError:
                raise
            # Any other failure here means "reconnect", not "crash the app".
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "broker_websocket_disconnected",
                    exception=repr(exc),
                    next_retry_in_seconds=backoff,
                )
                await self._sleep_or_stop(backoff)
                backoff = min(backoff * 2, self._max_backoff_seconds)
        logger.info("broker_websocket_stopped")

    async def _sleep_or_stop(self, backoff_seconds: float) -> None:
        """Wait out the backoff window, but wake immediately if `stop()`
        is called while waiting.
        """

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=backoff_seconds)
