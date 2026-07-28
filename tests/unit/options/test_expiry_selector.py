"""Unit tests for `ExpirySelector` (app.options.expiry_selector)."""

from datetime import date

import pytest

from app.options.exceptions import ExpiryNotFoundError
from app.options.expiry_selector import ExpirySelector
from app.options.models import ExpiryMode


def test_nearest_weekly_returns_first_upcoming_expiry() -> None:
    selector = ExpirySelector(ExpiryMode.NEAREST_WEEKLY)
    expiries = [date(2026, 8, 6), date(2026, 7, 30), date(2026, 8, 13)]

    result = selector.select(expiries, reference_date=date(2026, 7, 28))

    assert result == date(2026, 7, 30)


def test_next_weekly_returns_second_upcoming_expiry() -> None:
    selector = ExpirySelector(ExpiryMode.NEXT_WEEKLY)
    expiries = [date(2026, 8, 6), date(2026, 7, 30), date(2026, 8, 13)]

    result = selector.select(expiries, reference_date=date(2026, 7, 28))

    assert result == date(2026, 8, 6)


def test_next_weekly_raises_when_fewer_than_two_upcoming() -> None:
    selector = ExpirySelector(ExpiryMode.NEXT_WEEKLY)

    with pytest.raises(ExpiryNotFoundError):
        selector.select([date(2026, 7, 30)], reference_date=date(2026, 7, 28))


def test_monthly_returns_earliest_months_last_expiry() -> None:
    selector = ExpirySelector(ExpiryMode.MONTHLY)
    expiries = [
        date(2026, 7, 30),  # last Jul expiry
        date(2026, 7, 23),
        date(2026, 8, 27),  # last Aug expiry
        date(2026, 8, 6),
    ]

    result = selector.select(expiries, reference_date=date(2026, 7, 28))

    assert result == date(2026, 7, 30)


def test_monthly_with_single_expiry_in_a_month_uses_it() -> None:
    selector = ExpirySelector(ExpiryMode.MONTHLY)
    expiries = [date(2026, 7, 30), date(2026, 8, 27)]

    result = selector.select(expiries, reference_date=date(2026, 8, 1))

    assert result == date(2026, 8, 27)


def test_monthly_spanning_month_boundary_picks_correct_group() -> None:
    """A weekly expiry that falls in early August must not be lumped into
    July's group just because it's chronologically close to it.
    """

    selector = ExpirySelector(ExpiryMode.MONTHLY)
    expiries = [date(2026, 7, 30), date(2026, 8, 6), date(2026, 8, 13), date(2026, 8, 27)]

    result = selector.select(expiries, reference_date=date(2026, 7, 28))

    assert result == date(2026, 7, 30)


def test_select_excludes_expiries_before_reference_date() -> None:
    selector = ExpirySelector(ExpiryMode.NEAREST_WEEKLY)
    expiries = [date(2026, 7, 23), date(2026, 7, 30)]

    result = selector.select(expiries, reference_date=date(2026, 7, 28))

    assert result == date(2026, 7, 30)


def test_select_raises_on_empty_expiry_list() -> None:
    selector = ExpirySelector(ExpiryMode.NEAREST_WEEKLY)

    with pytest.raises(ExpiryNotFoundError):
        selector.select([], reference_date=date(2026, 7, 28))


def test_select_raises_when_all_expiries_are_in_the_past() -> None:
    selector = ExpirySelector(ExpiryMode.NEAREST_WEEKLY)

    with pytest.raises(ExpiryNotFoundError):
        selector.select([date(2026, 1, 1)], reference_date=date(2026, 7, 28))


def test_select_uses_default_mode_when_mode_omitted() -> None:
    selector = ExpirySelector(ExpiryMode.NEXT_WEEKLY)
    expiries = [date(2026, 7, 30), date(2026, 8, 6)]

    result = selector.select(expiries, reference_date=date(2026, 7, 28))

    assert result == date(2026, 8, 6)


def test_select_mode_argument_overrides_default_mode() -> None:
    selector = ExpirySelector(ExpiryMode.NEXT_WEEKLY)
    expiries = [date(2026, 7, 30), date(2026, 8, 6)]

    result = selector.select(expiries, mode=ExpiryMode.NEAREST_WEEKLY, reference_date=date(2026, 7, 28))

    assert result == date(2026, 7, 30)
