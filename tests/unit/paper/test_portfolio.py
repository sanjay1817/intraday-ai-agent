"""Unit tests for `app.paper.portfolio.PortfolioManager`."""

from datetime import UTC, datetime

import pytest

from app.domain.enums.trading import Exchange
from app.domain.exceptions.paper import InsufficientCashError
from app.paper.models import PaperPosition
from app.paper.portfolio import PortfolioManager

_NOW = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)


def make_position(**overrides: object) -> PaperPosition:
    defaults: dict[str, object] = {
        "symbol": "INFY-EQ",
        "exchange": Exchange.NSE,
        "quantity": 10,
        "average_price": 1500.0,
        "last_price": 1550.0,
        "unrealized_pnl": 500.0,
        "opened_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return PaperPosition(**defaults)  # type: ignore[arg-type]


# -- construction -----------------------------------------------------------------------


def test_construct_with_valid_initial_capital() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)

    assert portfolio.initial_capital == 100_000.0
    assert portfolio.cash == 100_000.0
    assert portfolio.available_cash == 100_000.0
    assert portfolio.reserved_cash == 0.0
    assert portfolio.realized_pnl == 0.0


@pytest.mark.parametrize("capital", [0.0, -1.0, -100_000.0])
def test_construct_rejects_non_positive_initial_capital(capital: float) -> None:
    with pytest.raises(ValueError, match="initial_capital"):
        PortfolioManager(initial_capital=capital)


# -- debit ------------------------------------------------------------------------------


async def test_debit_reduces_cash() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)

    await portfolio.debit(15_000.0)

    assert portfolio.cash == 85_000.0
    assert portfolio.available_cash == 85_000.0


async def test_debit_exact_cash_leaves_zero_balance() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)

    await portfolio.debit(100_000.0)

    assert portfolio.cash == 0.0


async def test_debit_more_than_cash_raises_insufficient_cash_error() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)

    with pytest.raises(InsufficientCashError) as exc_info:
        await portfolio.debit(100_000.01)

    assert exc_info.value.requested == 100_000.01
    assert exc_info.value.available == 100_000.0
    assert portfolio.cash == 100_000.0  # unchanged on rejection


async def test_debit_negative_amount_raises_value_error() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)

    with pytest.raises(ValueError, match="non-negative"):
        await portfolio.debit(-1.0)


async def test_debit_zero_is_a_no_op() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)

    await portfolio.debit(0.0)

    assert portfolio.cash == 100_000.0


# -- credit -----------------------------------------------------------------------------


async def test_credit_increases_cash() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)

    await portfolio.credit(5_000.0)

    assert portfolio.cash == 105_000.0


async def test_credit_negative_amount_raises_value_error() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)

    with pytest.raises(ValueError, match="non-negative"):
        await portfolio.credit(-1.0)


# -- reserve_cash / release_cash ---------------------------------------------------------


async def test_reserve_cash_reduces_available_cash_only() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)

    await portfolio.reserve_cash(20_000.0)

    assert portfolio.cash == 100_000.0
    assert portfolio.reserved_cash == 20_000.0
    assert portfolio.available_cash == 80_000.0


async def test_reserve_cash_more_than_available_raises() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)
    await portfolio.reserve_cash(80_000.0)

    with pytest.raises(InsufficientCashError) as exc_info:
        await portfolio.reserve_cash(20_000.01)

    assert exc_info.value.available == 20_000.0


async def test_reserve_cash_exact_available_succeeds() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)

    await portfolio.reserve_cash(100_000.0)

    assert portfolio.available_cash == 0.0


async def test_release_cash_restores_available_cash() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)
    await portfolio.reserve_cash(20_000.0)

    await portfolio.release_cash(20_000.0)

    assert portfolio.reserved_cash == 0.0
    assert portfolio.available_cash == 100_000.0


async def test_release_cash_more_than_reserved_raises_value_error() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)
    await portfolio.reserve_cash(10_000.0)

    with pytest.raises(ValueError, match="only 10000.0 is currently reserved"):
        await portfolio.release_cash(10_000.01)


async def test_release_cash_negative_amount_raises_value_error() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)

    with pytest.raises(ValueError, match="non-negative"):
        await portfolio.release_cash(-1.0)


async def test_reserve_then_fill_flow_releases_and_debits() -> None:
    """The typical resting-limit-order lifecycle: reserve worst-case cost
    up front, then on fill release the reservation and debit the actual
    (possibly different, for a STOP/trailing order) fill cost.
    """

    portfolio = PortfolioManager(initial_capital=100_000.0)
    await portfolio.reserve_cash(15_000.0)  # 10 shares @ limit price 1500

    await portfolio.release_cash(15_000.0)
    await portfolio.debit(14_950.0)  # actually filled at 1495

    assert portfolio.cash == 100_000.0 - 14_950.0
    assert portfolio.reserved_cash == 0.0
    assert portfolio.available_cash == portfolio.cash


# -- record_realized_pnl ------------------------------------------------------------------


async def test_record_realized_pnl_accumulates_profit() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)

    await portfolio.record_realized_pnl(500.0)
    await portfolio.record_realized_pnl(250.0)

    assert portfolio.realized_pnl == 750.0


async def test_record_realized_pnl_accumulates_loss() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)

    await portfolio.record_realized_pnl(-300.0)

    assert portfolio.realized_pnl == -300.0


async def test_record_realized_pnl_mixed_profit_and_loss_nets_out() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)

    await portfolio.record_realized_pnl(500.0)
    await portfolio.record_realized_pnl(-800.0)

    assert portfolio.realized_pnl == -300.0


# -- snapshot: zero positions ---------------------------------------------------------------


async def test_snapshot_with_zero_positions_is_flat() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)

    snapshot = await portfolio.snapshot([], now=_NOW)

    assert snapshot.cash == 100_000.0
    assert snapshot.available_cash == 100_000.0
    assert snapshot.used_capital == 0.0
    assert snapshot.realized_pnl == 0.0
    assert snapshot.unrealized_pnl == 0.0
    assert snapshot.total_pnl == 0.0
    assert snapshot.equity == 100_000.0
    assert snapshot.positions == []
    assert snapshot.updated_at == _NOW


async def test_snapshot_defaults_updated_at_to_now_when_unset() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)

    before = datetime.now(UTC)
    snapshot = await portfolio.snapshot([])
    after = datetime.now(UTC)

    assert before <= snapshot.updated_at <= after


# -- snapshot: buy / open position ------------------------------------------------------------


async def test_snapshot_reflects_an_open_buy_position_with_unrealized_profit() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)
    await portfolio.debit(10 * 1500.0)  # BUY 10 @ 1500

    position = make_position(quantity=10, average_price=1500.0, last_price=1550.0)
    snapshot = await portfolio.snapshot([position])

    assert snapshot.cash == 85_000.0
    assert snapshot.used_capital == 15_000.0
    assert snapshot.unrealized_pnl == 500.0
    assert snapshot.realized_pnl == 0.0
    assert snapshot.total_pnl == 500.0
    assert snapshot.equity == 85_000.0 + 10 * 1550.0


async def test_snapshot_reflects_an_open_buy_position_with_unrealized_loss() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)
    await portfolio.debit(10 * 1500.0)  # BUY 10 @ 1500

    position = make_position(
        quantity=10, average_price=1500.0, last_price=1450.0, unrealized_pnl=-500.0
    )
    snapshot = await portfolio.snapshot([position])

    assert snapshot.unrealized_pnl == -500.0
    assert snapshot.total_pnl == -500.0
    assert snapshot.equity == 85_000.0 + 10 * 1450.0


# -- snapshot: sell / close position -----------------------------------------------------------


async def test_snapshot_reflects_a_closed_long_position_with_realized_profit() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)
    await portfolio.debit(10 * 1500.0)  # open: BUY 10 @ 1500
    await portfolio.credit(10 * 1550.0)  # close: SELL 10 @ 1550
    await portfolio.record_realized_pnl(500.0)

    snapshot = await portfolio.snapshot([])  # position fully closed, no longer held

    assert snapshot.cash == 100_500.0
    assert snapshot.realized_pnl == 500.0
    assert snapshot.unrealized_pnl == 0.0
    assert snapshot.total_pnl == 500.0
    assert snapshot.equity == snapshot.cash
    assert snapshot.used_capital == 0.0


async def test_snapshot_reflects_a_closed_short_position_with_realized_loss() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)
    await portfolio.credit(10 * 1500.0)  # open: SELL (short) 10 @ 1500
    await portfolio.debit(10 * 1550.0)  # close: BUY to cover 10 @ 1550
    await portfolio.record_realized_pnl(-500.0)

    snapshot = await portfolio.snapshot([])

    assert snapshot.cash == 99_500.0
    assert snapshot.realized_pnl == -500.0
    assert snapshot.total_pnl == -500.0
    assert snapshot.equity == snapshot.cash


async def test_snapshot_reflects_a_partial_close() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)
    await portfolio.debit(10 * 1500.0)  # open 10 @ 1500
    await portfolio.credit(4 * 1550.0)  # close 4 of them @ 1550
    await portfolio.record_realized_pnl(4 * (1550.0 - 1500.0))

    remaining = make_position(
        quantity=6, average_price=1500.0, last_price=1550.0, unrealized_pnl=6 * 50.0
    )
    snapshot = await portfolio.snapshot([remaining])

    assert snapshot.realized_pnl == 200.0
    assert snapshot.unrealized_pnl == 300.0
    assert snapshot.total_pnl == 500.0
    assert snapshot.used_capital == 6 * 1500.0
    assert snapshot.cash == 100_000.0 - 15_000.0 + 4 * 1550.0


# -- snapshot: multiple positions --------------------------------------------------------------


async def test_snapshot_aggregates_multiple_positions_long_and_short() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)
    await portfolio.debit(10 * 1500.0)  # long INFY-EQ
    await portfolio.credit(5 * 3500.0)  # short TCS-EQ

    long_position = make_position(
        symbol="INFY-EQ", quantity=10, average_price=1500.0, last_price=1550.0
    )
    short_position = make_position(
        symbol="TCS-EQ", quantity=-5, average_price=3500.0, last_price=3400.0, unrealized_pnl=500.0
    )
    snapshot = await portfolio.snapshot([long_position, short_position])

    assert snapshot.used_capital == 10 * 1500.0 + 5 * 3500.0
    assert snapshot.unrealized_pnl == 1000.0
    assert len(snapshot.positions) == 2


# -- snapshot: reservation reflected in available_cash --------------------------------------------


async def test_snapshot_available_cash_reflects_reservation() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)
    await portfolio.reserve_cash(30_000.0)  # a resting limit order

    snapshot = await portfolio.snapshot([])

    assert snapshot.cash == 100_000.0
    assert snapshot.available_cash == 70_000.0


# -- reconciliation across a full multi-step scenario ------------------------------------------------


async def test_portfolio_reconciles_across_a_full_scenario() -> None:
    """Buy, partially close for a profit, open a short, reserve cash for
    a resting order, then snapshot — every derived figure must still
    reconcile (proven by `Portfolio`'s own construction-time invariants:
    this test would raise `ValidationError` if any of them drifted).
    """

    portfolio = PortfolioManager(initial_capital=200_000.0)

    await portfolio.debit(20 * 1500.0)  # BUY 20 INFY-EQ @ 1500
    await portfolio.credit(8 * 1600.0)  # SELL 8 of them @ 1600
    await portfolio.record_realized_pnl(8 * (1600.0 - 1500.0))

    await portfolio.credit(15 * 800.0)  # SELL (short) 15 RELIANCE-EQ @ 800

    await portfolio.reserve_cash(10_000.0)  # a resting order elsewhere

    infy_remaining = make_position(
        symbol="INFY-EQ",
        quantity=12,
        average_price=1500.0,
        last_price=1520.0,
        unrealized_pnl=12 * 20.0,
    )
    reliance_short = make_position(
        symbol="RELIANCE-EQ",
        quantity=-15,
        average_price=800.0,
        last_price=790.0,
        unrealized_pnl=150.0,
    )

    snapshot = await portfolio.snapshot([infy_remaining, reliance_short], now=_NOW)

    expected_cash = 200_000.0 - 20 * 1500.0 + 8 * 1600.0 + 15 * 800.0
    assert snapshot.cash == expected_cash
    assert snapshot.available_cash == expected_cash - 10_000.0
    assert snapshot.used_capital == 12 * 1500.0 + 15 * 800.0
    assert snapshot.realized_pnl == 800.0
    assert snapshot.unrealized_pnl == 12 * 20.0 + 150.0
    assert snapshot.total_pnl == snapshot.realized_pnl + snapshot.unrealized_pnl
    market_value = 12 * 1520.0 + (-15 * 790.0)
    assert snapshot.equity == expected_cash + market_value


# -- reset ------------------------------------------------------------------------------------


async def test_reset_restores_initial_capital() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)
    await portfolio.debit(50_000.0)
    await portfolio.record_realized_pnl(-1000.0)
    await portfolio.reserve_cash(10_000.0)

    await portfolio.reset()

    assert portfolio.cash == 100_000.0
    assert portfolio.available_cash == 100_000.0
    assert portfolio.reserved_cash == 0.0
    assert portfolio.realized_pnl == 0.0


async def test_reset_with_new_capital_overrides_default() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)

    await portfolio.reset(initial_capital=250_000.0)

    assert portfolio.initial_capital == 250_000.0
    assert portfolio.cash == 250_000.0


async def test_reset_new_capital_becomes_the_default_for_a_later_no_arg_reset() -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)
    await portfolio.reset(initial_capital=250_000.0)
    await portfolio.debit(50_000.0)

    await portfolio.reset()

    assert portfolio.cash == 250_000.0


@pytest.mark.parametrize("capital", [0.0, -1.0])
async def test_reset_rejects_non_positive_capital(capital: float) -> None:
    portfolio = PortfolioManager(initial_capital=100_000.0)

    with pytest.raises(ValueError, match="initial_capital"):
        await portfolio.reset(initial_capital=capital)

    assert portfolio.cash == 100_000.0  # unchanged on rejection
