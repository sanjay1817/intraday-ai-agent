"""Domain-level exceptions.

These represent business-rule violations (e.g. "user already exists") and
carry no HTTP or transport awareness. `app.core.exception_handlers` maps
them onto HTTP responses at the API boundary.
"""

from app.domain.exceptions.broker import (
    BrokerAPIError,
    BrokerAuthenticationError,
    BrokerConnectionError,
    BrokerError,
    OrderRejectionError,
    TokenExpiredError,
    WebSocketError,
)
from app.domain.exceptions.indicators import (
    IndicatorError,
    InsufficientDataError,
    InvalidOHLCVDataError,
    UnknownIndicatorError,
)
from app.domain.exceptions.market import (
    InvalidHistoricalDataError,
    MarketDataError,
    NoHistoricalDataError,
)
from app.domain.exceptions.paper import (
    InsufficientCashError,
    InvalidOrderStateError,
    OrderNotFoundError,
    PaperTradingError,
)
from app.domain.exceptions.research import (
    ExperimentTrackingError,
    FeatureEngineeringError,
    HyperparameterOptimizationError,
    MissingFeatureSourceError,
    ResearchError,
    UnknownRunError,
)

__all__ = [
    "BrokerAPIError",
    "BrokerAuthenticationError",
    "BrokerConnectionError",
    "BrokerError",
    "ExperimentTrackingError",
    "FeatureEngineeringError",
    "HyperparameterOptimizationError",
    "IndicatorError",
    "InsufficientCashError",
    "InsufficientDataError",
    "InvalidHistoricalDataError",
    "InvalidOHLCVDataError",
    "InvalidOrderStateError",
    "MarketDataError",
    "MissingFeatureSourceError",
    "NoHistoricalDataError",
    "OrderNotFoundError",
    "OrderRejectionError",
    "PaperTradingError",
    "ResearchError",
    "TokenExpiredError",
    "UnknownIndicatorError",
    "UnknownRunError",
    "WebSocketError",
]
