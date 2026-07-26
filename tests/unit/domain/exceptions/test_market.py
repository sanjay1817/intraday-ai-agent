"""Unit tests for `app.domain.exceptions.market`."""

from app.domain.enums.trading import BrokerName, Exchange, HistoricalInterval
from app.domain.exceptions import (
    InvalidHistoricalDataError,
    MarketDataError,
    NoHistoricalDataError,
)


def test_no_historical_data_error_is_a_market_data_error() -> None:
    assert issubclass(NoHistoricalDataError, MarketDataError)


def test_invalid_historical_data_error_is_a_market_data_error() -> None:
    assert issubclass(InvalidHistoricalDataError, MarketDataError)


def test_market_data_error_is_a_plain_exception_base() -> None:
    assert issubclass(MarketDataError, Exception)
    assert MarketDataError.__bases__ == (Exception,)


def test_no_historical_data_error_carries_structured_context() -> None:
    error = NoHistoricalDataError(
        broker=BrokerName.ZERODHA,
        exchange=Exchange.NSE,
        tradingsymbol="256265",
        interval=HistoricalInterval.FIVE_MINUTE,
    )

    assert error.broker is BrokerName.ZERODHA
    assert error.exchange is Exchange.NSE
    assert error.tradingsymbol == "256265"
    assert error.interval is HistoricalInterval.FIVE_MINUTE
    assert "zerodha" in str(error)
    assert "NSE:256265" in str(error)
    assert "5minute" in str(error)


def test_invalid_historical_data_error_carries_structured_context() -> None:
    error = InvalidHistoricalDataError(
        broker=BrokerName.UPSTOX,
        exchange=Exchange.NSE,
        tradingsymbol="NSE_EQ|INE002A01018",
        reason="high (90.0) is below low (95.0)",
    )

    assert error.broker is BrokerName.UPSTOX
    assert error.exchange is Exchange.NSE
    assert error.tradingsymbol == "NSE_EQ|INE002A01018"
    assert error.reason == "high (90.0) is below low (95.0)"
    assert "upstox" in str(error)
    assert "high (90.0) is below low (95.0)" in str(error)


def test_exceptions_are_re_exported_identically_from_the_package() -> None:
    from app.domain import exceptions as package
    from app.domain.exceptions import market as module

    assert package.NoHistoricalDataError is module.NoHistoricalDataError
    assert package.InvalidHistoricalDataError is module.InvalidHistoricalDataError
    assert package.MarketDataError is module.MarketDataError
