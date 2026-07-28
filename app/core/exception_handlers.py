"""Maps this codebase's domain exceptions onto HTTP responses.

Every business layer already raises its own framework-agnostic exception
(`BrokerError` from `app.brokers`, `IndicatorError` from `app.indicators`,
`ResearchError` from `app.research`, `MarketDataError` from
`app.market.ingestion`) with no knowledge of HTTP — that's deliberate
(see each exception module's own docstring: domain code shouldn't import
FastAPI). This module is the *one* place that decides what HTTP
status/body a given domain failure becomes, so route handlers never
need their own try/except boilerplate around calls into those layers.

FastAPI/Starlette dispatch an exception to the most specific handler
registered for any class in `type(exc).__mro__`, so registering a single
shared handler against each family's *base* class (`BrokerError`,
`IndicatorError`, `ResearchError`, `MarketDataError`) is enough to cover
every subclass automatically — `_resolve_status_code` then picks the
precise status code for whichever concrete subclass was actually
raised, without a handler needing to be registered per subclass.

Every handled exception's extra attributes (e.g. `UnknownIndicatorError`'s
`available` list, `BrokerAPIError`'s `error_code`) are surfaced in the
response body under `error.detail` — these fields exist specifically so
"callers ... can suggest a correction" (see
`app.domain.exceptions.indicators`), so withholding them from the actual
caller would defeat their purpose. An unhandled, non-domain exception is
the one case that must NOT reach the client with any detail: its message
and traceback are logged server-side only, and the response body is a
fixed, generic message.
"""

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from structlog import get_logger

from app.domain.exceptions import (
    BrokerError,
    IndicatorError,
    MarketDataError,
    PaperTradingError,
    ResearchError,
)
from app.domain.exceptions.broker import (
    BrokerConnectionError,
    OrderRejectionError,
    WebSocketError,
)
from app.domain.exceptions.indicators import InsufficientDataError
from app.domain.exceptions.market import InvalidHistoricalDataError, NoHistoricalDataError
from app.domain.exceptions.paper import (
    InsufficientCashError,
    InvalidOrderStateError,
    OrderNotFoundError,
)
from app.domain.exceptions.research import UnknownRunError
from app.options.exceptions import (
    ExpiryNotFoundError,
    InvalidLotQuantityError,
    InvalidOptionChainDataError,
    OptionChainFetchError,
    OptionPositionNotFoundError,
    OptionRiskLimitExceededError,
    OptionsError,
    OptionsInfrastructureUnavailableError,
    PremiumUnavailableError,
    StrikeNotFoundError,
    TradingModeNotEnabledError,
    UnsupportedUnderlyingError,
)

logger = get_logger(__name__)

#: Ordered most-specific-first: `_resolve_status_code` returns the status
#: code for the first entry `exc` is an instance of, so a subclass listed
#: here overrides its base class's entry, and every unlisted subclass
#: falls through to its nearest listed ancestor.
_STATUS_CODE_BY_EXCEPTION: tuple[tuple[type[Exception], int], ...] = (
    (OrderRejectionError, 422),  # the broker understood and rejected the order
    (BrokerConnectionError, 503),  # transient network failure, retryable
    (WebSocketError, 503),  # streaming connection unavailable
    (BrokerError, 502),  # broker integration failed generically
    (InsufficientDataError, 422),  # well-formed request, not enough data to satisfy it
    (IndicatorError, 400),  # the indicator request itself was invalid
    (UnknownRunError, 404),  # referenced a run ID that doesn't exist
    (ResearchError, 422),  # request couldn't be computed/run as specified
    (NoHistoricalDataError, 404),  # the broker had no data for this symbol/range
    (InvalidHistoricalDataError, 502),  # the broker returned malformed OHLCV data
    (MarketDataError, 502),  # market-data ingestion failed generically
    (OrderNotFoundError, 404),  # referenced a paper order id that doesn't exist
    (InvalidOrderStateError, 409),  # the paper order isn't in a cancellable/modifiable state
    (InsufficientCashError, 422),  # well-formed request, not enough paper capital to satisfy it
    (PaperTradingError, 400),  # paper trading engine failed generically
    (TradingModeNotEnabledError, 400),  # options endpoint called without TRADING_MODE=OPTIONS
    (UnsupportedUnderlyingError, 400),  # requested underlying isn't in the configured set
    (ExpiryNotFoundError, 404),  # no expiry available/matching the requested mode
    (StrikeNotFoundError, 404),  # no strike available for the request
    (PremiumUnavailableError, 502),  # broker LTP lookup for the selected contract failed
    (OptionsInfrastructureUnavailableError, 503),  # no OptionChainService configured (non-Angel-One broker)
    (InvalidOptionChainDataError, 502),  # chain data parsed to empty/nonsensical
    (OptionChainFetchError, 503),  # broker chain fetch failed (network/API)
    (OptionPositionNotFoundError, 404),  # no open option position to exit
    (InvalidLotQuantityError, 400),  # malformed lots request
    (OptionRiskLimitExceededError, 422),  # well-formed request, rejected by an options risk limit
    (OptionsError, 400),  # options integration failed generically
)


def _resolve_status_code(exc: Exception) -> int:
    """The HTTP status code for `exc`, per `_STATUS_CODE_BY_EXCEPTION`."""

    for exc_type, status_code in _STATUS_CODE_BY_EXCEPTION:
        if isinstance(exc, exc_type):
            return status_code
    return 500


def _json_serializable_detail(exc: Exception) -> dict[str, Any]:
    """`exc`'s instance attributes, filtered to those that survive
    round-tripping through `json.dumps` — every domain exception sets
    these in its own `__init__` (e.g. `broker`, `error_code`,
    `available`), and they're plain strings/numbers/lists by design.
    """

    detail: dict[str, Any] = {}
    for key, value in vars(exc).items():
        try:
            json.dumps(value)
        except TypeError:
            continue
        detail[key] = value
    return detail


async def _handle_domain_error(request: Request, exc: Exception) -> JSONResponse:
    """Shared handler for every `BrokerError`/`IndicatorError`/
    `ResearchError`/`MarketDataError`.
    """

    status_code = _resolve_status_code(exc)
    detail = _json_serializable_detail(exc)

    logger.warning(
        "domain_error",
        exception_type=type(exc).__name__,
        http_status_code=status_code,
        method=request.method,
        path=request.url.path,
        message=str(exc),
        detail=detail,
    )

    error_body: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)}
    if detail:
        error_body["detail"] = detail

    return JSONResponse(status_code=status_code, content={"error": error_body})


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler for anything that isn't one of this project's
    own domain exceptions — a programming bug, not a business-rule
    failure. The real exception (with traceback) is logged server-side
    only; the client gets a fixed, generic message, never `str(exc)` or
    any of its attributes, since an arbitrary exception's message may
    contain internal detail that was never designed to be client-facing.
    """

    logger.error(
        "unhandled_exception",
        exception_type=type(exc).__name__,
        method=request.method,
        path=request.url.path,
        exc_info=exc,
    )

    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "type": "InternalServerError",
                "message": "An unexpected error occurred.",
            }
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Wire every handler in this module into `app`.

    Registering against each family's base class (`BrokerError`,
    `IndicatorError`, `ResearchError`, `MarketDataError`,
    `PaperTradingError`, `OptionsError`) is sufficient for every present
    and future subclass — see this module's docstring for why.
    """

    app.add_exception_handler(BrokerError, _handle_domain_error)
    app.add_exception_handler(IndicatorError, _handle_domain_error)
    app.add_exception_handler(ResearchError, _handle_domain_error)
    app.add_exception_handler(MarketDataError, _handle_domain_error)
    app.add_exception_handler(PaperTradingError, _handle_domain_error)
    app.add_exception_handler(OptionsError, _handle_domain_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
