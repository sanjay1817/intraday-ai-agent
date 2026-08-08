"""Unit tests for `app.backtest.dto`: request validation."""

from datetime import date, time

import pytest

from app.backtest.dto import BacktestRequest
from app.domain.enums.trading import Exchange
from app.domain.exceptions.backtest import FutureDateError


def _request(**overrides: object) -> BacktestRequest:
    defaults: dict[object, object] = dict(
        symbol="RELIANCE-EQ",
        exchange=Exchange.NSE,
        historical_date=date(2026, 8, 5),
        initial_capital=50_000.0,
    )
    defaults.update(overrides)
    return BacktestRequest.model_validate(defaults)


def test_check_not_future_dated_rejects_today() -> None:
    request = _request(historical_date=date(2026, 8, 8))

    with pytest.raises(FutureDateError):
        request.check_not_future_dated(today=date(2026, 8, 8))


def test_check_not_future_dated_rejects_a_date_after_today() -> None:
    request = _request(historical_date=date(2026, 8, 9))

    with pytest.raises(FutureDateError):
        request.check_not_future_dated(today=date(2026, 8, 8))


def test_check_not_future_dated_accepts_a_past_date() -> None:
    request = _request(historical_date=date(2026, 8, 5))

    request.check_not_future_dated(today=date(2026, 8, 8))  # must not raise


def test_start_time_must_be_before_end_time() -> None:
    with pytest.raises(ValueError, match="start_time must be before end_time"):
        _request(start_time=time(15, 30), end_time=time(9, 15))


def test_default_time_window_is_a_full_nse_session() -> None:
    request = _request()

    assert request.start_time == time(9, 15)
    assert request.end_time == time(15, 30)
