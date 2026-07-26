"""Unit tests for `app.domain.exceptions.paper`."""

from app.domain.exceptions import InsufficientCashError, PaperTradingError


def test_insufficient_cash_error_is_a_paper_trading_error() -> None:
    assert issubclass(InsufficientCashError, PaperTradingError)


def test_paper_trading_error_is_a_plain_exception_base() -> None:
    assert issubclass(PaperTradingError, Exception)
    assert PaperTradingError.__bases__ == (Exception,)


def test_insufficient_cash_error_carries_structured_context() -> None:
    error = InsufficientCashError(
        "paper: insufficient cash: requested 15000.0, available 10000.0",
        requested=15000.0,
        available=10000.0,
    )

    assert error.requested == 15000.0
    assert error.available == 10000.0
    assert "insufficient cash" in str(error)


def test_exceptions_are_re_exported_identically_from_the_package() -> None:
    from app.domain import exceptions as package
    from app.domain.exceptions import paper as module

    assert package.InsufficientCashError is module.InsufficientCashError
    assert package.PaperTradingError is module.PaperTradingError
