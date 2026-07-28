"""Unit tests for `app.options.risk.OptionRiskManager` — mirrors
`tests/unit/auto/test_risk.py`'s structure/day-rollover coverage for
`AutoRiskManager`.
"""

from datetime import UTC, datetime, timedelta

from app.options.risk import OptionRiskManager

_T0 = datetime(2024, 1, 1, 9, 15, tzinfo=UTC)


def make_manager(**overrides: object) -> OptionRiskManager:
    kwargs: dict = {
        "max_lots_per_order": 10,
        "max_premium_per_order": 50_000.0,
        "max_premium_exposure": 200_000.0,
        "max_daily_loss": 10_000.0,
    }
    kwargs.update(overrides)
    return OptionRiskManager(**kwargs)  # type: ignore[arg-type]


# -- allowed by default -----------------------------------------------------------------


def test_order_allowed_with_no_activity() -> None:
    manager = make_manager()

    result = manager.check_order_allowed(
        lots=1, order_premium_value=1000.0, current_premium_exposure=0.0, now=_T0
    )

    assert result.allowed is True
    assert result.reason is None


# -- max lots per order -------------------------------------------------------------------


def test_max_lots_per_order_blocks_when_exceeded() -> None:
    manager = make_manager(max_lots_per_order=5)

    result = manager.check_order_allowed(
        lots=6, order_premium_value=1000.0, current_premium_exposure=0.0, now=_T0
    )

    assert result.allowed is False
    assert "option_max_lots_per_order" in (result.reason or "")


def test_max_lots_per_order_allows_at_the_limit() -> None:
    manager = make_manager(max_lots_per_order=5)

    result = manager.check_order_allowed(
        lots=5, order_premium_value=1000.0, current_premium_exposure=0.0, now=_T0
    )

    assert result.allowed is True


# -- max premium per order ------------------------------------------------------------------


def test_max_premium_per_order_blocks_when_exceeded() -> None:
    manager = make_manager(max_premium_per_order=10_000.0)

    result = manager.check_order_allowed(
        lots=1, order_premium_value=10_001.0, current_premium_exposure=0.0, now=_T0
    )

    assert result.allowed is False
    assert "option_max_premium_per_order" in (result.reason or "")


# -- max premium exposure -------------------------------------------------------------------


def test_max_premium_exposure_blocks_when_exceeded() -> None:
    manager = make_manager(max_premium_exposure=20_000.0)

    result = manager.check_order_allowed(
        lots=1, order_premium_value=15_000.0, current_premium_exposure=10_000.0, now=_T0
    )

    assert result.allowed is False
    assert "option_max_premium_exposure" in (result.reason or "")


def test_max_premium_exposure_allows_when_within_limit() -> None:
    manager = make_manager(max_premium_exposure=20_000.0)

    result = manager.check_order_allowed(
        lots=1, order_premium_value=5_000.0, current_premium_exposure=10_000.0, now=_T0
    )

    assert result.allowed is True


# -- max daily loss ---------------------------------------------------------------------------


def test_max_daily_loss_blocks_once_breached() -> None:
    manager = make_manager(max_daily_loss=1000.0)
    manager.record_trade_closed(-1000.0, now=_T0)

    result = manager.check_order_allowed(
        lots=1, order_premium_value=1000.0, current_premium_exposure=0.0, now=_T0
    )

    assert result.allowed is False
    assert "option_max_daily_loss" in (result.reason or "")


def test_max_daily_loss_not_breached_by_a_smaller_loss() -> None:
    manager = make_manager(max_daily_loss=1000.0)
    manager.record_trade_closed(-500.0, now=_T0)

    result = manager.check_order_allowed(
        lots=1, order_premium_value=1000.0, current_premium_exposure=0.0, now=_T0
    )

    assert result.allowed is True


def test_profits_do_not_trip_max_daily_loss() -> None:
    manager = make_manager(max_daily_loss=1000.0)
    manager.record_trade_closed(5000.0, now=_T0)

    result = manager.check_order_allowed(
        lots=1, order_premium_value=1000.0, current_premium_exposure=0.0, now=_T0
    )

    assert result.allowed is True


def test_daily_realized_pnl_nets_profits_and_losses() -> None:
    manager = make_manager()
    manager.record_trade_closed(500.0, now=_T0)
    manager.record_trade_closed(-200.0, now=_T0)

    assert manager.daily_realized_pnl == 300.0


# -- ordering ---------------------------------------------------------------------------------


def test_check_ordering_reports_daily_loss_before_other_reasons() -> None:
    """When multiple conditions are simultaneously true, `reason` must
    name the first one actually checked (daily-loss lockout).
    """

    manager = make_manager(max_daily_loss=100.0, max_lots_per_order=1)
    manager.record_trade_closed(-100.0, now=_T0)

    result = manager.check_order_allowed(
        lots=99, order_premium_value=999_999.0, current_premium_exposure=999_999.0, now=_T0
    )

    assert result.allowed is False
    assert "option_max_daily_loss" in (result.reason or "")


def test_check_ordering_reports_lots_before_premium_checks() -> None:
    manager = make_manager(max_lots_per_order=1, max_premium_per_order=100.0)

    result = manager.check_order_allowed(
        lots=2, order_premium_value=999_999.0, current_premium_exposure=0.0, now=_T0
    )

    assert result.allowed is False
    assert "option_max_lots_per_order" in (result.reason or "")


def test_check_ordering_reports_per_order_premium_before_exposure() -> None:
    manager = make_manager(max_premium_per_order=100.0, max_premium_exposure=100.0)

    result = manager.check_order_allowed(
        lots=1, order_premium_value=999_999.0, current_premium_exposure=999_999.0, now=_T0
    )

    assert result.allowed is False
    assert "option_max_premium_per_order" in (result.reason or "")


# -- day rollover -----------------------------------------------------------------------------


def test_daily_realized_pnl_resets_on_a_new_ist_day() -> None:
    manager = make_manager()
    manager.record_trade_closed(-500.0, now=_T0)
    assert manager.daily_realized_pnl == -500.0

    next_day = _T0 + timedelta(days=1)
    manager.record_trade_closed(100.0, now=next_day)

    assert manager.daily_realized_pnl == 100.0  # not -400


def test_daily_loss_lockout_clears_on_a_new_ist_day() -> None:
    manager = make_manager(max_daily_loss=1000.0)
    manager.record_trade_closed(-1000.0, now=_T0)
    locked_out = manager.check_order_allowed(
        lots=1, order_premium_value=100.0, current_premium_exposure=0.0, now=_T0
    )
    assert locked_out.allowed is False

    next_day = _T0 + timedelta(days=1)
    result = manager.check_order_allowed(
        lots=1, order_premium_value=100.0, current_premium_exposure=0.0, now=next_day
    )

    assert result.allowed is True


def test_day_rollover_uses_ist_not_utc_boundary() -> None:
    """23:00 UTC on day 1 is already 04:30 IST on day 2 -- the day must
    roll over on the IST boundary, not the UTC one.
    """

    manager = make_manager()
    late_utc = datetime(2024, 1, 1, 23, 0, tzinfo=UTC)  # 2024-01-02 04:30 IST
    manager.record_trade_closed(-100.0, now=late_utc)
    assert manager.daily_realized_pnl == -100.0

    same_ist_day_later = datetime(2024, 1, 2, 1, 0, tzinfo=UTC)  # 2024-01-02 06:30 IST
    manager.record_trade_closed(-50.0, now=same_ist_day_later)

    assert manager.daily_realized_pnl == -150.0  # still the same IST trading day


# -- reset --------------------------------------------------------------------------------------


def test_reset_clears_every_counter() -> None:
    manager = make_manager(max_daily_loss=100.0)
    manager.record_trade_closed(-100.0, now=_T0)
    assert manager.daily_realized_pnl == -100.0

    manager.reset()

    assert manager.daily_realized_pnl == 0.0
    result = manager.check_order_allowed(
        lots=1, order_premium_value=100.0, current_premium_exposure=0.0, now=_T0
    )
    assert result.allowed is True
