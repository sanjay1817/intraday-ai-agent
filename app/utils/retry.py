"""Shared retry policy for outbound broker HTTP calls.

Centralized here so every broker adapter retries transient failures the
same way — only `BaseBrokerAdapter._request` (app/brokers/base.py) calls
this; it must never be reimplemented per adapter.
"""

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.domain.exceptions.broker import BrokerConnectionError

logger = structlog.get_logger(__name__)

#: Exceptions considered transient and safe to retry. Authentication
#: failures, token expiry, and business-level API rejections are
#: deliberately excluded — retrying those would either be pointless (bad
#: credentials won't fix themselves) or dangerous (resending an order
#: rejected for a business reason).
_RETRYABLE_EXCEPTIONS = (BrokerConnectionError, httpx.TransportError)


def build_retrying(
    max_attempts: int, initial_backoff_seconds: float, max_backoff_seconds: float
) -> AsyncRetrying:
    """Build a `tenacity.AsyncRetrying` policy for one broker request.

    Args:
        max_attempts: Total attempts including the first (i.e. `retries + 1`).
        initial_backoff_seconds: Wait before the second attempt; doubles
            each subsequent attempt (exponential backoff).
        max_backoff_seconds: Ceiling on the exponential wait.
    """

    return AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=initial_backoff_seconds, max=max_backoff_seconds),
        retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
        reraise=True,
        before_sleep=_log_retry_attempt,
    )


def _log_retry_attempt(retry_state: RetryCallState) -> None:
    """Emit a structured warning before each backoff sleep."""

    exception = retry_state.outcome.exception() if retry_state.outcome else None
    logger.warning(
        "broker_request_retry",
        attempt=retry_state.attempt_number,
        exception=repr(exception),
    )
