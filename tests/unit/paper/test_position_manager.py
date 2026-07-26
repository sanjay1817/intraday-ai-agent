"""Unit tests for `app.paper.position_manager.PositionManager`."""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.enums.trading import Exchange, OrderSide
from app.paper.models import TradeMetadata
from app.paper.position_manager import PositionManager

_T0 = datetime(2024, 1, 1, 9, 15, tzinfo=UTC)


def _later(minutes: int) -> datetime:
    return _T0 + timedelta(minutes=minutes)


# -- opening ------------------------------------------------------------------------------


async def test_fresh_buy_opens_a_long_position() -> None:
    manager = PositionManager()

    outcome = await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=10,
        price=1500.0,
        order_id="o1",
        timestamp=_T0,
    )

    assert outcome.closed_lots == []
    assert outcome.realized_pnl_delta == 0.0
    assert outcome.position is not None
    assert outcome.position.quantity == 10
    assert outcome.position.average_price == 1500.0
    assert outcome.position.last_price == 1500.0
    assert outcome.position.unrealized_pnl == 0.0
    assert outcome.position.realized_pnl == 0.0
    assert outcome.position.opened_at == _T0


async def test_fresh_sell_opens_a_short_position() -> None:
    manager = PositionManager()

    outcome = await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.SELL,
        quantity=10,
        price=1500.0,
        order_id="o1",
        timestamp=_T0,
    )

    assert outcome.position is not None
    assert outcome.position.quantity == -10
    assert outcome.position.average_price == 1500.0


# -- adding to an existing position (same direction) ---------------------------------------


async def test_adding_to_a_long_position_computes_weighted_average() -> None:
    manager = PositionManager()
    await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=10,
        price=1500.0,
        order_id="o1",
        timestamp=_T0,
    )

    outcome = await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=10,
        price=1600.0,
        order_id="o2",
        timestamp=_later(1),
    )

    assert outcome.closed_lots == []
    assert outcome.position is not None
    assert outcome.position.quantity == 20
    assert outcome.position.average_price == 1550.0  # (10*1500 + 10*1600) / 20
    assert outcome.position.opened_at == _T0  # earliest lot still open


async def test_adding_to_a_short_position_computes_weighted_average() -> None:
    manager = PositionManager()
    await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.SELL,
        quantity=10,
        price=1500.0,
        order_id="o1",
        timestamp=_T0,
    )
    outcome = await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.SELL,
        quantity=10,
        price=1400.0,
        order_id="o2",
        timestamp=_later(1),
    )

    assert outcome.position is not None
    assert outcome.position.quantity == -20
    assert outcome.position.average_price == 1450.0


# -- partial close --------------------------------------------------------------------------


async def test_partial_close_of_a_long_position_realizes_profit() -> None:
    manager = PositionManager()
    await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=10,
        price=1500.0,
        order_id="o1",
        timestamp=_T0,
    )

    outcome = await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.SELL,
        quantity=4,
        price=1550.0,
        order_id="o2",
        timestamp=_later(5),
    )

    assert outcome.realized_pnl_delta == 200.0  # 4 * (1550 - 1500)
    assert len(outcome.closed_lots) == 1
    closed = outcome.closed_lots[0]
    assert closed.entry_order_id == "o1"
    assert closed.entry_price == 1500.0
    assert closed.entry_timestamp == _T0
    assert closed.quantity == 4
    assert closed.exit_price == 1550.0
    assert closed.realized_pnl == 200.0

    assert outcome.position is not None
    assert outcome.position.quantity == 6
    assert outcome.position.average_price == 1500.0  # unchanged: same lot, just smaller
    assert outcome.position.realized_pnl == 200.0
    assert outcome.position.unrealized_pnl == 6 * (1550.0 - 1500.0)


async def test_partial_close_of_a_short_position_realizes_profit() -> None:
    manager = PositionManager()
    await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.SELL,
        quantity=10,
        price=1500.0,
        order_id="o1",
        timestamp=_T0,
    )

    outcome = await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=4,
        price=1450.0,
        order_id="o2",
        timestamp=_later(5),
    )

    assert outcome.realized_pnl_delta == 200.0  # 4 * (1500 - 1450)
    assert outcome.position is not None
    assert outcome.position.quantity == -6


async def test_partial_close_realizes_a_loss() -> None:
    manager = PositionManager()
    await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=10,
        price=1500.0,
        order_id="o1",
        timestamp=_T0,
    )

    outcome = await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.SELL,
        quantity=4,
        price=1450.0,
        order_id="o2",
        timestamp=_later(5),
    )

    assert outcome.realized_pnl_delta == -200.0  # 4 * (1450 - 1500)


# -- exact close ------------------------------------------------------------------------------


async def test_exact_close_removes_the_position() -> None:
    manager = PositionManager()
    await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=10,
        price=1500.0,
        order_id="o1",
        timestamp=_T0,
    )

    outcome = await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.SELL,
        quantity=10,
        price=1600.0,
        order_id="o2",
        timestamp=_later(5),
    )

    assert outcome.position is None
    assert outcome.realized_pnl_delta == 1000.0
    assert await manager.get_position("INFY-EQ", Exchange.NSE) is None


# -- flip through flat --------------------------------------------------------------------------


async def test_flip_closes_old_lifecycle_and_opens_a_fresh_one() -> None:
    manager = PositionManager()
    await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=10,
        price=1500.0,
        order_id="o1",
        timestamp=_T0,
    )

    outcome = await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.SELL,
        quantity=15,
        price=1600.0,
        order_id="o2",
        timestamp=_later(5),
    )

    # The old long (10 @ 1500) is fully closed, realizing 10*(1600-1500)=1000.
    assert outcome.realized_pnl_delta == 1000.0
    assert len(outcome.closed_lots) == 1
    assert outcome.closed_lots[0].quantity == 10

    # A brand new short of 5 opens at 1600, with its OWN fresh lifecycle
    # (realized_pnl == 0.0, not the 1000 the old position just realized).
    assert outcome.position is not None
    assert outcome.position.quantity == -5
    assert outcome.position.average_price == 1600.0
    assert outcome.position.realized_pnl == 0.0
    assert outcome.position.opened_at == _later(5)


async def test_position_realized_pnl_resets_after_a_flip_on_the_next_fill() -> None:
    """A further fill against the new (post-flip) side must accumulate
    from 0, not silently inherit the old lifecycle's realized P&L.
    """

    manager = PositionManager()
    await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=10,
        price=1500.0,
        order_id="o1",
        timestamp=_T0,
    )
    await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.SELL,
        quantity=15,
        price=1600.0,
        order_id="o2",
        timestamp=_later(5),
    )  # flips to short 5 @ 1600

    outcome = await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=2,
        price=1550.0,
        order_id="o3",
        timestamp=_later(10),
    )  # covers 2 of the new short at a profit

    assert outcome.realized_pnl_delta == 100.0  # 2 * (1600 - 1550)
    assert outcome.position is not None
    assert outcome.position.realized_pnl == 100.0  # not 1000 + 100


# -- FIFO across multiple lots ------------------------------------------------------------------


async def test_close_spans_multiple_lots_fifo_order() -> None:
    manager = PositionManager()
    await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=5,
        price=1400.0,
        order_id="entry-1",
        timestamp=_T0,
    )
    await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=5,
        price=1600.0,
        order_id="entry-2",
        timestamp=_later(1),
    )

    outcome = await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.SELL,
        quantity=7,
        price=1500.0,
        order_id="exit-1",
        timestamp=_later(5),
    )

    assert len(outcome.closed_lots) == 2
    first, second = outcome.closed_lots
    assert first.entry_order_id == "entry-1"
    assert first.quantity == 5
    assert first.realized_pnl == 5 * (1500.0 - 1400.0)
    assert second.entry_order_id == "entry-2"
    assert second.quantity == 2
    assert second.realized_pnl == 2 * (1500.0 - 1600.0)
    assert outcome.realized_pnl_delta == first.realized_pnl + second.realized_pnl

    assert outcome.position is not None
    assert outcome.position.quantity == 3
    assert outcome.position.average_price == 1600.0  # remainder of entry-2's lot
    assert outcome.position.opened_at == _later(1)  # entry-1 fully closed, no longer "earliest"


async def test_close_carries_entry_metadata_onto_the_closed_lot() -> None:
    manager = PositionManager()
    metadata = TradeMetadata(confidence=82.0, agreeing_strategies=["ema_trend"])
    await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=10,
        price=1500.0,
        order_id="o1",
        timestamp=_T0,
        metadata=metadata,
    )

    outcome = await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.SELL,
        quantity=10,
        price=1550.0,
        order_id="o2",
        timestamp=_later(5),
    )

    assert outcome.closed_lots[0].metadata is metadata


# -- validation -----------------------------------------------------------------------------------


@pytest.mark.parametrize("quantity", [0, -1])
async def test_apply_fill_rejects_non_positive_quantity(quantity: int) -> None:
    manager = PositionManager()

    with pytest.raises(ValueError, match="quantity"):
        await manager.apply_fill(
            symbol="INFY-EQ",
            exchange=Exchange.NSE,
            side=OrderSide.BUY,
            quantity=quantity,
            price=1500.0,
            order_id="o1",
            timestamp=_T0,
        )


@pytest.mark.parametrize("price", [0.0, -1.0])
async def test_apply_fill_rejects_non_positive_price(price: float) -> None:
    manager = PositionManager()

    with pytest.raises(ValueError, match="price"):
        await manager.apply_fill(
            symbol="INFY-EQ",
            exchange=Exchange.NSE,
            side=OrderSide.BUY,
            quantity=10,
            price=price,
            order_id="o1",
            timestamp=_T0,
        )


# -- update_last_price ---------------------------------------------------------------------------


async def test_update_last_price_recomputes_unrealized_pnl_only() -> None:
    manager = PositionManager()
    await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=10,
        price=1500.0,
        order_id="o1",
        timestamp=_T0,
    )
    await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.SELL,
        quantity=4,
        price=1550.0,
        order_id="o2",
        timestamp=_later(1),
    )  # realizes 200, leaves 6 open

    position = await manager.update_last_price("INFY-EQ", Exchange.NSE, 1600.0, now=_later(10))

    assert position is not None
    assert position.last_price == 1600.0
    assert position.unrealized_pnl == 6 * (1600.0 - 1500.0)
    assert position.realized_pnl == 200.0  # untouched by a pure mark
    assert position.updated_at == _later(10)


async def test_update_last_price_with_no_open_position_is_a_no_op() -> None:
    manager = PositionManager()

    result = await manager.update_last_price("INFY-EQ", Exchange.NSE, 1600.0)

    assert result is None


async def test_update_last_price_rejects_non_positive_price() -> None:
    manager = PositionManager()

    with pytest.raises(ValueError, match="price"):
        await manager.update_last_price("INFY-EQ", Exchange.NSE, 0.0)


# -- lookups / multi-symbol isolation / reset ------------------------------------------------------


async def test_positions_for_different_symbols_are_isolated() -> None:
    manager = PositionManager()
    await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=10,
        price=1500.0,
        order_id="o1",
        timestamp=_T0,
    )
    await manager.apply_fill(
        symbol="TCS-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.SELL,
        quantity=5,
        price=3500.0,
        order_id="o2",
        timestamp=_T0,
    )

    positions = await manager.get_all_positions()

    assert {p.symbol for p in positions} == {"INFY-EQ", "TCS-EQ"}
    infy = await manager.get_position("INFY-EQ", Exchange.NSE)
    assert infy is not None
    assert infy.quantity == 10


async def test_same_symbol_different_exchange_is_a_distinct_position() -> None:
    manager = PositionManager()
    await manager.apply_fill(
        symbol="RELIANCE",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=10,
        price=2500.0,
        order_id="o1",
        timestamp=_T0,
    )
    await manager.apply_fill(
        symbol="RELIANCE",
        exchange=Exchange.BSE,
        side=OrderSide.BUY,
        quantity=5,
        price=2501.0,
        order_id="o2",
        timestamp=_T0,
    )

    nse_position = await manager.get_position("RELIANCE", Exchange.NSE)
    bse_position = await manager.get_position("RELIANCE", Exchange.BSE)

    assert nse_position is not None and nse_position.quantity == 10
    assert bse_position is not None and bse_position.quantity == 5


async def test_get_position_for_unknown_symbol_returns_none() -> None:
    manager = PositionManager()

    assert await manager.get_position("NOSUCH-EQ", Exchange.NSE) is None


async def test_get_all_positions_empty_initially() -> None:
    manager = PositionManager()

    assert await manager.get_all_positions() == []


async def test_reset_clears_all_positions() -> None:
    manager = PositionManager()
    await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=10,
        price=1500.0,
        order_id="o1",
        timestamp=_T0,
    )

    await manager.reset()

    assert await manager.get_all_positions() == []
    assert await manager.get_position("INFY-EQ", Exchange.NSE) is None


async def test_reset_then_reopening_a_symbol_starts_a_fresh_lifecycle() -> None:
    manager = PositionManager()
    await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=10,
        price=1500.0,
        order_id="o1",
        timestamp=_T0,
    )
    await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.SELL,
        quantity=10,
        price=1600.0,
        order_id="o2",
        timestamp=_later(1),
    )
    await manager.reset()

    outcome = await manager.apply_fill(
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=3,
        price=1700.0,
        order_id="o3",
        timestamp=_later(2),
    )

    assert outcome.position is not None
    assert outcome.position.realized_pnl == 0.0
    assert outcome.position.quantity == 3
