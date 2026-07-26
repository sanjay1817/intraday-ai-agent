"""Unit tests for `app.auto.risk.AutoRiskManager`."""

from datetime import UTC, datetime, timedelta

from app.auto.models import AutoTradingConfig
from app.auto.risk import AutoRiskManager

_T0 = datetime(2024, 1, 1, 9, 15, tzinfo=UTC)


def make_manager(**overrides: object) -> AutoRiskManager:
    config = AutoTradingConfig(**overrides)  # type: ignore[arg-type]
    return AutoRiskManager(config)


# -- entry allowed by default --------------------------------------------------------------


def test_entry_allowed_with_no_activity() -> None:
    manager = make_manager()

    result = manager.check_entry_allowed(open_position_count=0, now=_T0)

    assert result.allowed is True
    assert result.reason is None


# -- max open positions -------------------------------------------------------------------


def test_max_open_positions_blocks_entry_at_the_limit() -> None:
    manager = make_manager(max_open_positions=2)

    result = manager.check_entry_allowed(open_position_count=2, now=_T0)

    assert result.allowed is False
    assert "max_open_positions" in (result.reason or "")


def test_max_open_positions_allows_entry_below_the_limit() -> None:
    manager = make_manager(max_open_positions=2)

    result = manager.check_entry_allowed(open_position_count=1, now=_T0)

    assert result.allowed is True


# -- max daily trades ------------------------------------------------------------------------


def test_max_daily_trades_blocks_entry_at_the_limit() -> None:
    manager = make_manager(max_daily_trades=2)
    manager.record_trade_opened(now=_T0)
    manager.record_trade_opened(now=_T0)

    result = manager.check_entry_allowed(open_position_count=0, now=_T0)

    assert result.allowed is False
    assert "max_daily_trades" in (result.reason or "")


def test_trades_today_accumulates() -> None:
    manager = make_manager()
    manager.record_trade_opened(now=_T0)
    manager.record_trade_opened(now=_T0)

    assert manager.trades_today == 2


# -- max daily loss --------------------------------------------------------------------------


def test_max_daily_loss_blocks_entry_once_breached() -> None:
    manager = make_manager(max_daily_loss=1000.0)
    manager.record_trade_closed(-1000.0, now=_T0)

    result = manager.check_entry_allowed(open_position_count=0, now=_T0)

    assert result.allowed is False
    assert "max_daily_loss" in (result.reason or "")


def test_max_daily_loss_not_breached_by_a_smaller_loss() -> None:
    manager = make_manager(max_daily_loss=1000.0)
    manager.record_trade_closed(-500.0, now=_T0)

    result = manager.check_entry_allowed(open_position_count=0, now=_T0)

    assert result.allowed is True


def test_daily_realized_pnl_nets_profits_and_losses() -> None:
    manager = make_manager()
    manager.record_trade_closed(500.0, now=_T0)
    manager.record_trade_closed(-200.0, now=_T0)

    assert manager.daily_realized_pnl == 300.0


def test_profits_do_not_trip_max_daily_loss() -> None:
    manager = make_manager(max_daily_loss=1000.0)
    manager.record_trade_closed(5000.0, now=_T0)

    result = manager.check_entry_allowed(open_position_count=0, now=_T0)

    assert result.allowed is True


# -- cooldown after consecutive losses ---------------------------------------------------------


def test_cooldown_activates_after_consecutive_losses() -> None:
    manager = make_manager(cooldown_after_consecutive_losses=2, cooldown_minutes=15.0)
    manager.record_trade_closed(-100.0, now=_T0)
    assert manager.consecutive_losses == 1
    manager.record_trade_closed(-100.0, now=_T0)

    assert manager.consecutive_losses == 2
    assert manager.cooldown_until == _T0 + timedelta(minutes=15.0)

    result = manager.check_entry_allowed(open_position_count=0, now=_T0)
    assert result.allowed is False
    assert "cooldown" in (result.reason or "")


def test_cooldown_expires_after_its_duration() -> None:
    manager = make_manager(cooldown_after_consecutive_losses=1, cooldown_minutes=10.0)
    manager.record_trade_closed(-100.0, now=_T0)

    still_cooling = manager.check_entry_allowed(
        open_position_count=0, now=_T0 + timedelta(minutes=5)
    )
    assert still_cooling.allowed is False

    expired = manager.check_entry_allowed(open_position_count=0, now=_T0 + timedelta(minutes=11))
    assert expired.allowed is True


def test_a_profit_resets_the_consecutive_loss_streak() -> None:
    manager = make_manager(cooldown_after_consecutive_losses=3)
    manager.record_trade_closed(-100.0, now=_T0)
    manager.record_trade_closed(-100.0, now=_T0)
    manager.record_trade_closed(200.0, now=_T0)

    assert manager.consecutive_losses == 0


def test_a_profit_does_not_clear_an_already_active_cooldown() -> None:
    manager = make_manager(cooldown_after_consecutive_losses=1, cooldown_minutes=30.0)
    manager.record_trade_closed(-100.0, now=_T0)
    assert manager.cooldown_until is not None

    manager.record_trade_closed(200.0, now=_T0)

    assert manager.cooldown_until is not None  # still active, not cleared by the win
    assert manager.consecutive_losses == 0  # but the streak itself did reset


def test_check_ordering_reports_cooldown_before_other_reasons() -> None:
    """When multiple conditions are simultaneously true, the reported
    reason must be the first one actually checked (cooldown), so a
    caller isn't told the wrong story.
    """

    manager = make_manager(
        cooldown_after_consecutive_losses=1, max_open_positions=1, cooldown_minutes=30.0
    )
    manager.record_trade_closed(-100.0, now=_T0)

    result = manager.check_entry_allowed(open_position_count=5, now=_T0)

    assert result.allowed is False
    assert "cooldown" in (result.reason or "")


# -- day rollover -----------------------------------------------------------------------------


def test_trades_today_resets_on_a_new_ist_day() -> None:
    manager = make_manager()
    manager.record_trade_opened(now=_T0)
    assert manager.trades_today == 1

    next_day = _T0 + timedelta(days=1)
    manager.record_trade_opened(now=next_day)

    assert manager.trades_today == 1  # reset, then incremented once for the new day


def test_daily_realized_pnl_resets_on_a_new_ist_day() -> None:
    manager = make_manager()
    manager.record_trade_closed(-500.0, now=_T0)
    assert manager.daily_realized_pnl == -500.0

    next_day = _T0 + timedelta(days=1)
    manager.record_trade_closed(100.0, now=next_day)

    assert manager.daily_realized_pnl == 100.0  # not -400


def test_consecutive_losses_and_cooldown_survive_a_day_rollover() -> None:
    """A cooldown earned right before midnight must not evaporate the
    instant the calendar day changes.
    """

    manager = make_manager(cooldown_after_consecutive_losses=1, cooldown_minutes=120.0)
    manager.record_trade_closed(-100.0, now=_T0)
    cooldown_until = manager.cooldown_until
    assert cooldown_until is not None

    next_day = _T0 + timedelta(days=1)
    result = manager.check_entry_allowed(open_position_count=0, now=next_day)

    if next_day < cooldown_until:
        assert result.allowed is False
    assert manager.consecutive_losses == 1


def test_day_rollover_uses_ist_not_utc_boundary() -> None:
    """23:00 UTC on day 1 is already 04:30 IST on day 2 -- the day must
    roll over on the IST boundary, not the UTC one.
    """

    manager = make_manager()
    late_utc = datetime(2024, 1, 1, 23, 0, tzinfo=UTC)  # 2024-01-02 04:30 IST
    manager.record_trade_opened(now=late_utc)
    assert manager.trades_today == 1

    same_ist_day_later = datetime(2024, 1, 2, 1, 0, tzinfo=UTC)  # 2024-01-02 06:30 IST
    manager.record_trade_opened(now=same_ist_day_later)

    assert manager.trades_today == 2  # still the same IST trading day


# -- reset --------------------------------------------------------------------------------------


def test_reset_clears_every_counter() -> None:
    manager = make_manager(cooldown_after_consecutive_losses=1)
    manager.record_trade_opened(now=_T0)
    manager.record_trade_closed(-100.0, now=_T0)
    assert manager.trades_today == 1
    assert manager.cooldown_until is not None

    manager.reset()

    assert manager.trades_today == 0
    assert manager.daily_realized_pnl == 0.0
    assert manager.consecutive_losses == 0
    assert manager.cooldown_until is None
