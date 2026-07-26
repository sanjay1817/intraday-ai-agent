"""Broker exception hierarchy.

`BaseBrokerAdapter._request` (app/brokers/base.py) is the single place
that translates transport failures and broker error payloads into these
exceptions, so every adapter and every caller reacts to the same set of
error types regardless of which broker raised them.
"""

from app.domain.enums.trading import BrokerName


class BrokerError(Exception):
    """Base class for every broker-related failure."""

    def __init__(self, message: str, *, broker: BrokerName | None = None) -> None:
        self.broker = broker
        super().__init__(message)


class BrokerConnectionError(BrokerError):
    """Raised when a request could not reach the broker after all retries
    (network failure, timeout, or DNS error) — not a broker-returned error.
    """


class BrokerAuthenticationError(BrokerError):
    """Raised when login fails outright (bad credentials, bad TOTP, etc.).

    Unlike `TokenExpiredError`, this is not recoverable by refreshing a
    token — the caller must re-authenticate from scratch.
    """


class TokenExpiredError(BrokerAuthenticationError):
    """Raised when the broker rejects a request because the access token
    has expired or is invalid. `BaseBrokerAdapter._request` catches this,
    calls `refresh_token()` once, and retries the original request.
    """


class BrokerAPIError(BrokerError):
    """Raised when the broker's API responds with a business-level error
    (e.g. invalid instrument, malformed payload) that isn't a token or
    auth problem and should not be retried.
    """

    def __init__(
        self,
        message: str,
        *,
        broker: BrokerName | None = None,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(message, broker=broker)


class OrderRejectionError(BrokerAPIError):
    """Raised when the broker explicitly rejects an order placement,
    modification, or cancellation request.
    """


class InstrumentNotFoundError(BrokerAPIError):
    """Raised when a broker-specific instrument lookup (e.g. Angel One's
    trading-symbol -> `symboltoken` resolution against its instrument
    master) finds no match for the requested exchange/trading symbol.
    """


class WebSocketError(BrokerError):
    """Raised for streaming connection failures that exhaust the
    reconnect policy in `app.brokers.ws_client.ReconnectingWebSocketClient`.
    """
