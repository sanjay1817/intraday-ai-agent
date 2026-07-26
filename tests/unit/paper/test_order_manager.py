"""Unit tests for `app.paper.order_manager.OrderManager`.

Uses real `PortfolioManager`/`PositionManager` instances throughout
(both already independently tested) rather than mocks — this is
deliberately an integration-style test of the settlement pipeline
(reserve/debit/credit/apply-fill/record-P&L all actually happening),
since that pipeline's correctness is the entire point of this class.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.enums.trading import Exchange, OrderSide, OrderStatus, OrderValidity
from app.domain.exceptions.paper import (
    InsufficientCashError,
    InvalidOrderStateError,
    OrderNotFoundError,
)
from app.paper.dto import OrderModificationRequest, PaperOrderRequest, PaperOrderType
from app.paper.order_manager import OrderManager
from app.paper.portfolio import PortfolioManager
from app.paper.position_manager import PositionManager

_T0 = datetime(2024, 1, 1, 9, 15, tzinfo=UTC)


def _later(minutes: int) -> datetime:
    return _T0 + timedelta(minutes=minutes)


def make_manager(
    initial_capital: float = 100_000.0,
) -> tuple[OrderManager, PortfolioManager, PositionManager]:
    portfolio = PortfolioManager(initial_capital=initial_capital)
    positions = PositionManager()
    return OrderManager(portfolio, positions), portfolio, positions


def market_request(
    symbol: str = "INFY-EQ",
    side: OrderSide = OrderSide.BUY,
    quantity: int = 10,
    **overrides: object,
) -> PaperOrderRequest:
    defaults: dict[str, object] = {
        "symbol": symbol,
        "exchange": Exchange.NSE,
        "side": side,
        "order_type": PaperOrderType.MARKET,
        "quantity": quantity,
    }
    defaults.update(overrides)
    return PaperOrderRequest(**defaults)  # type: ignore[arg-type]


def limit_request(
    symbol: str = "INFY-EQ",
    side: OrderSide = OrderSide.BUY,
    quantity: int = 10,
    price: float = 1500.0,
    **overrides: object,
) -> PaperOrderRequest:
    defaults: dict[str, object] = {
        "symbol": symbol,
        "exchange": Exchange.NSE,
        "side": side,
        "order_type": PaperOrderType.LIMIT,
        "quantity": quantity,
        "price": price,
    }
    defaults.update(overrides)
    return PaperOrderRequest(**defaults)  # type: ignore[arg-type]


def stop_request(
    symbol: str = "INFY-EQ",
    side: OrderSide = OrderSide.BUY,
    quantity: int = 10,
    trigger_price: float = 1550.0,
    **overrides: object,
) -> PaperOrderRequest:
    defaults: dict[str, object] = {
        "symbol": symbol,
        "exchange": Exchange.NSE,
        "side": side,
        "order_type": PaperOrderType.STOP,
        "quantity": quantity,
        "trigger_price": trigger_price,
    }
    defaults.update(overrides)
    return PaperOrderRequest(**defaults)  # type: ignore[arg-type]


# -- MARKET orders ------------------------------------------------------------------------


async def test_market_buy_fills_immediately_and_debits_cash() -> None:
    manager, portfolio, positions = make_manager()

    order, trades = await manager.place_order(market_request(), current_price=1500.0, now=_T0)

    assert order.status == OrderStatus.COMPLETE
    assert order.filled_quantity == 10
    assert order.average_fill_price == 1500.0
    assert trades == []
    assert portfolio.cash == 100_000.0 - 15_000.0
    position = await positions.get_position("INFY-EQ", Exchange.NSE)
    assert position is not None
    assert position.quantity == 10


async def test_market_sell_short_fills_immediately_and_credits_cash() -> None:
    manager, portfolio, positions = make_manager()

    order, _ = await manager.place_order(
        market_request(side=OrderSide.SELL), current_price=1500.0, now=_T0
    )

    assert order.status == OrderStatus.COMPLETE
    assert portfolio.cash == 100_000.0 + 15_000.0
    position = await positions.get_position("INFY-EQ", Exchange.NSE)
    assert position is not None
    assert position.quantity == -10


async def test_market_sell_closing_a_long_produces_a_closed_trade() -> None:
    manager, portfolio, _ = make_manager()
    await manager.place_order(market_request(), current_price=1500.0, now=_T0)

    order, trades = await manager.place_order(
        market_request(side=OrderSide.SELL), current_price=1600.0, now=_later(5)
    )

    assert order.status == OrderStatus.COMPLETE
    assert len(trades) == 1
    trade = trades[0]
    assert trade.side == OrderSide.BUY
    assert trade.entry_price == 1500.0
    assert trade.exit_price == 1600.0
    assert trade.quantity == 10
    assert trade.pnl == 1000.0
    assert trade.exit_timestamp == _later(5)
    assert portfolio.realized_pnl == 1000.0


async def test_market_buy_insufficient_cash_is_rejected_not_raised() -> None:
    manager, portfolio, positions = make_manager(initial_capital=1000.0)

    order, trades = await manager.place_order(
        market_request(quantity=10), current_price=1500.0, now=_T0
    )

    assert order.status == OrderStatus.REJECTED
    assert order.status_message is not None
    assert trades == []
    assert portfolio.cash == 1000.0  # untouched
    assert await positions.get_position("INFY-EQ", Exchange.NSE) is None


async def test_place_order_rejects_non_positive_current_price() -> None:
    manager, _, _ = make_manager()

    with pytest.raises(ValueError, match="current_price"):
        await manager.place_order(market_request(), current_price=0.0)


# -- LIMIT orders --------------------------------------------------------------------------


async def test_limit_buy_not_marketable_rests_open_and_reserves_cash() -> None:
    manager, portfolio, _ = make_manager()

    order, trades = await manager.place_order(
        limit_request(price=1400.0), current_price=1500.0, now=_T0
    )

    assert order.status == OrderStatus.OPEN
    assert trades == []
    assert portfolio.available_cash == 100_000.0 - 14_000.0
    assert portfolio.cash == 100_000.0  # not yet actually spent


async def test_limit_buy_immediately_marketable_fills_at_current_price() -> None:
    manager, portfolio, _ = make_manager()

    order, _ = await manager.place_order(limit_request(price=1500.0), current_price=1450.0, now=_T0)

    assert order.status == OrderStatus.COMPLETE
    assert order.average_fill_price == 1450.0  # fills at the (better) current price
    assert portfolio.cash == 100_000.0 - 14_500.0
    assert portfolio.available_cash == portfolio.cash  # reservation released


async def test_limit_sell_not_marketable_rests_without_reserving_cash() -> None:
    manager, portfolio, positions = make_manager()
    await manager.place_order(market_request(), current_price=1500.0, now=_T0)  # own 10 first

    order, _ = await manager.place_order(
        limit_request(side=OrderSide.SELL, price=1600.0), current_price=1500.0, now=_later(1)
    )

    assert order.status == OrderStatus.OPEN
    # SELL orders never reserve cash in this engine.
    assert portfolio.available_cash == portfolio.cash


async def test_limit_buy_insufficient_cash_to_reserve_is_rejected() -> None:
    manager, portfolio, _ = make_manager(initial_capital=1000.0)

    order, _ = await manager.place_order(
        limit_request(price=1400.0, quantity=10), current_price=1500.0, now=_T0
    )

    assert order.status == OrderStatus.REJECTED
    assert portfolio.available_cash == 1000.0


async def test_resting_limit_fills_on_a_later_price_update() -> None:
    manager, portfolio, _ = make_manager()
    order, _ = await manager.place_order(limit_request(price=1400.0), current_price=1500.0, now=_T0)
    assert order.status == OrderStatus.OPEN

    trades = await manager.process_price_update("INFY-EQ", Exchange.NSE, 1390.0, now=_later(5))

    updated = await manager.get_order(order.order_id)
    assert updated is not None
    assert updated.status == OrderStatus.COMPLETE
    assert updated.average_fill_price == 1390.0
    assert trades == []
    assert portfolio.cash == 100_000.0 - 13_900.0


async def test_price_update_for_a_different_symbol_does_not_affect_resting_order() -> None:
    manager, _, _ = make_manager()
    order, _ = await manager.place_order(
        limit_request(symbol="INFY-EQ", price=1400.0), current_price=1500.0, now=_T0
    )

    await manager.process_price_update("TCS-EQ", Exchange.NSE, 1.0, now=_later(1))

    unchanged = await manager.get_order(order.order_id)
    assert unchanged is not None
    assert unchanged.status == OrderStatus.OPEN


async def test_process_price_update_rejects_non_positive_price() -> None:
    manager, _, _ = make_manager()

    with pytest.raises(ValueError, match="price"):
        await manager.process_price_update("INFY-EQ", Exchange.NSE, 0.0)


# -- STOP orders ---------------------------------------------------------------------------


async def test_stop_buy_not_triggered_rests_trigger_pending() -> None:
    manager, portfolio, _ = make_manager()

    order, _ = await manager.place_order(
        stop_request(trigger_price=1550.0), current_price=1500.0, now=_T0
    )

    assert order.status == OrderStatus.TRIGGER_PENDING
    assert portfolio.available_cash == 100_000.0 - 15_500.0


async def test_stop_buy_triggered_immediately_fills() -> None:
    manager, _, _ = make_manager()

    order, _ = await manager.place_order(
        stop_request(trigger_price=1550.0), current_price=1560.0, now=_T0
    )

    assert order.status == OrderStatus.COMPLETE
    assert order.average_fill_price == 1560.0


async def test_stop_sell_triggers_when_price_falls_to_or_below() -> None:
    manager, _, positions = make_manager()
    await manager.place_order(market_request(quantity=10), current_price=1500.0, now=_T0)

    order, _ = await manager.place_order(
        stop_request(side=OrderSide.SELL, trigger_price=1450.0, quantity=10),
        current_price=1500.0,
        now=_later(1),
    )
    assert order.status == OrderStatus.TRIGGER_PENDING

    await manager.process_price_update("INFY-EQ", Exchange.NSE, 1440.0, now=_later(2))

    updated = await manager.get_order(order.order_id)
    assert updated is not None
    assert updated.status == OrderStatus.COMPLETE
    position = await positions.get_position("INFY-EQ", Exchange.NSE)
    assert position is None  # closed out


# -- STOP_LIMIT orders -----------------------------------------------------------------------


async def test_stop_limit_waits_for_trigger_then_rests_as_limit() -> None:
    manager, _, _ = make_manager()

    order, _ = await manager.place_order(
        PaperOrderRequest(
            symbol="INFY-EQ",
            exchange=Exchange.NSE,
            side=OrderSide.BUY,
            order_type=PaperOrderType.STOP_LIMIT,
            quantity=10,
            price=1560.0,
            trigger_price=1550.0,
        ),
        current_price=1500.0,
        now=_T0,
    )
    assert order.status == OrderStatus.TRIGGER_PENDING

    await manager.process_price_update("INFY-EQ", Exchange.NSE, 1555.0, now=_later(1))
    triggered = await manager.get_order(order.order_id)
    assert triggered is not None
    # Triggered (>=1550) and marketable (555 <= limit 1560) in the same update -> fills.
    assert triggered.status == OrderStatus.COMPLETE
    assert triggered.average_fill_price == 1555.0


async def test_stop_limit_triggers_but_not_yet_marketable_rests_open() -> None:
    manager, _, _ = make_manager()

    order, _ = await manager.place_order(
        PaperOrderRequest(
            symbol="INFY-EQ",
            exchange=Exchange.NSE,
            side=OrderSide.BUY,
            order_type=PaperOrderType.STOP_LIMIT,
            quantity=10,
            price=1550.0,
            trigger_price=1550.0,
        ),
        current_price=1500.0,
        now=_T0,
    )

    await manager.process_price_update("INFY-EQ", Exchange.NSE, 1560.0, now=_later(1))
    triggered = await manager.get_order(order.order_id)
    assert triggered is not None
    assert triggered.status == OrderStatus.OPEN  # triggered, but 1560 > limit 1550

    await manager.process_price_update("INFY-EQ", Exchange.NSE, 1540.0, now=_later(2))
    filled = await manager.get_order(order.order_id)
    assert filled is not None
    assert filled.status == OrderStatus.COMPLETE
    assert filled.average_fill_price == 1540.0


# -- TRAILING_STOP orders ------------------------------------------------------------------------


async def test_trailing_stop_sell_computes_initial_trigger_from_amount() -> None:
    manager, _, positions = make_manager()
    await manager.place_order(market_request(quantity=10), current_price=1500.0, now=_T0)

    order, _ = await manager.place_order(
        PaperOrderRequest(
            symbol="INFY-EQ",
            exchange=Exchange.NSE,
            side=OrderSide.SELL,
            order_type=PaperOrderType.TRAILING_STOP,
            quantity=10,
            trailing_amount=50.0,
        ),
        current_price=1500.0,
        now=_later(1),
    )

    assert order.status == OrderStatus.TRIGGER_PENDING
    assert order.trigger_price == 1450.0


async def test_trailing_stop_sell_ratchets_up_but_never_down() -> None:
    manager, _, _ = make_manager()
    await manager.place_order(market_request(quantity=10), current_price=1500.0, now=_T0)
    order, _ = await manager.place_order(
        PaperOrderRequest(
            symbol="INFY-EQ",
            exchange=Exchange.NSE,
            side=OrderSide.SELL,
            order_type=PaperOrderType.TRAILING_STOP,
            quantity=10,
            trailing_amount=50.0,
        ),
        current_price=1500.0,
        now=_later(1),
    )
    assert order.trigger_price == 1450.0

    # Price rises: trigger should ratchet up to 1550 - 50 = 1500.
    await manager.process_price_update("INFY-EQ", Exchange.NSE, 1550.0, now=_later(2))
    risen = await manager.get_order(order.order_id)
    assert risen is not None
    assert risen.trigger_price == 1500.0
    assert risen.status == OrderStatus.TRIGGER_PENDING  # 1550 > 1500, not triggered yet

    # Price pulls back but stays above the ratcheted trigger: trigger must NOT retreat.
    await manager.process_price_update("INFY-EQ", Exchange.NSE, 1520.0, now=_later(3))
    pulled_back = await manager.get_order(order.order_id)
    assert pulled_back is not None
    assert pulled_back.trigger_price == 1500.0  # unchanged, not 1470
    assert pulled_back.status == OrderStatus.TRIGGER_PENDING

    # Price falls through the (ratcheted, not the original) trigger -> fills.
    await manager.process_price_update("INFY-EQ", Exchange.NSE, 1495.0, now=_later(4))
    filled = await manager.get_order(order.order_id)
    assert filled is not None
    assert filled.status == OrderStatus.COMPLETE
    assert filled.average_fill_price == 1495.0


async def test_trailing_stop_buy_computes_initial_trigger_from_percent() -> None:
    manager, _, _ = make_manager()

    order, _ = await manager.place_order(
        PaperOrderRequest(
            symbol="INFY-EQ",
            exchange=Exchange.NSE,
            side=OrderSide.BUY,
            order_type=PaperOrderType.TRAILING_STOP,
            quantity=10,
            trailing_percent=10.0,
        ),
        current_price=1000.0,
        now=_T0,
    )

    assert order.trigger_price == 1100.0  # 1000 * (1 + 10/100)


# -- cancel ----------------------------------------------------------------------------------------


async def test_cancel_open_order_releases_reservation() -> None:
    manager, portfolio, _ = make_manager()
    order, _ = await manager.place_order(limit_request(price=1400.0), current_price=1500.0, now=_T0)
    assert portfolio.available_cash < portfolio.cash

    cancelled = await manager.cancel_order(order.order_id, now=_later(1))

    assert cancelled.status == OrderStatus.CANCELLED
    assert portfolio.available_cash == portfolio.cash


async def test_cancel_unknown_order_raises_order_not_found() -> None:
    manager, _, _ = make_manager()

    with pytest.raises(OrderNotFoundError):
        await manager.cancel_order("no-such-id")


async def test_cancel_already_complete_order_raises_invalid_state() -> None:
    manager, _, _ = make_manager()
    order, _ = await manager.place_order(market_request(), current_price=1500.0, now=_T0)

    with pytest.raises(InvalidOrderStateError):
        await manager.cancel_order(order.order_id)


# -- modify ------------------------------------------------------------------------------------------


async def test_modify_order_quantity_adjusts_reservation() -> None:
    manager, portfolio, _ = make_manager()
    order, _ = await manager.place_order(
        limit_request(price=1500.0, quantity=10), current_price=1600.0, now=_T0
    )
    assert portfolio.available_cash == 100_000.0 - 15_000.0

    updated = await manager.modify_order(
        OrderModificationRequest(order_id=order.order_id, quantity=20), now=_later(1)
    )

    assert updated.quantity == 20
    assert portfolio.available_cash == 100_000.0 - 30_000.0


async def test_modify_order_insufficient_cash_restores_original_reservation() -> None:
    manager, portfolio, _ = make_manager(initial_capital=20_000.0)
    order, _ = await manager.place_order(
        limit_request(price=1500.0, quantity=10), current_price=1600.0, now=_T0
    )
    available_before = portfolio.available_cash

    with pytest.raises(InsufficientCashError):
        await manager.modify_order(
            OrderModificationRequest(order_id=order.order_id, quantity=100), now=_later(1)
        )

    assert portfolio.available_cash == available_before
    unchanged = await manager.get_order(order.order_id)
    assert unchanged is not None
    assert unchanged.quantity == 10


async def test_modify_unknown_order_raises_order_not_found() -> None:
    manager, _, _ = make_manager()

    with pytest.raises(OrderNotFoundError):
        await manager.modify_order(OrderModificationRequest(order_id="no-such-id", quantity=5))


async def test_modify_non_resting_order_raises_invalid_state() -> None:
    manager, _, _ = make_manager()
    order, _ = await manager.place_order(market_request(), current_price=1500.0, now=_T0)

    with pytest.raises(InvalidOrderStateError):
        await manager.modify_order(OrderModificationRequest(order_id=order.order_id, quantity=5))


async def test_modify_order_updates_validity_and_expiry() -> None:
    manager, _, _ = make_manager()
    order, _ = await manager.place_order(limit_request(price=1400.0), current_price=1500.0, now=_T0)
    new_expiry = _later(120)

    updated = await manager.modify_order(
        OrderModificationRequest(
            order_id=order.order_id,
            validity=OrderValidity.IMMEDIATE_OR_CANCEL,
            expires_at=new_expiry,
        )
    )

    assert updated.validity == OrderValidity.IMMEDIATE_OR_CANCEL
    assert updated.expires_at == new_expiry


# -- bracket / OCO -----------------------------------------------------------------------------------


async def test_bracket_entry_spawns_stop_and_target_legs_on_fill() -> None:
    manager, _, _ = make_manager()

    entry, _ = await manager.place_order(
        market_request(quantity=10, stop_loss_price=1450.0, target_price=1600.0),
        current_price=1500.0,
        now=_T0,
    )
    assert entry.status == OrderStatus.COMPLETE

    all_orders = await manager.get_all_orders()
    children = [o for o in all_orders if o.parent_order_id == entry.order_id]
    assert len(children) == 2
    stop_leg = next(o for o in children if o.order_type is PaperOrderType.STOP)
    target_leg = next(o for o in children if o.order_type is PaperOrderType.LIMIT)
    assert stop_leg.trigger_price == 1450.0
    assert stop_leg.side == OrderSide.SELL
    assert target_leg.price == 1600.0
    assert target_leg.side == OrderSide.SELL
    assert stop_leg.oco_group_id == target_leg.oco_group_id
    assert stop_leg.status == OrderStatus.TRIGGER_PENDING
    assert target_leg.status == OrderStatus.OPEN


async def test_bracket_target_fill_cancels_the_stop_leg() -> None:
    manager, portfolio, positions = make_manager()
    entry, _ = await manager.place_order(
        market_request(quantity=10, stop_loss_price=1450.0, target_price=1600.0),
        current_price=1500.0,
        now=_T0,
    )
    all_orders = await manager.get_all_orders()
    stop_leg = next(
        o
        for o in all_orders
        if o.parent_order_id == entry.order_id and o.order_type is PaperOrderType.STOP
    )

    trades = await manager.process_price_update("INFY-EQ", Exchange.NSE, 1600.0, now=_later(5))

    updated_stop = await manager.get_order(stop_leg.order_id)
    assert updated_stop is not None
    assert updated_stop.status == OrderStatus.CANCELLED
    assert updated_stop.status_message == "cancelled: OCO sibling filled"
    assert len(trades) == 1
    assert trades[0].pnl == 10 * (1600.0 - 1500.0)
    assert await positions.get_position("INFY-EQ", Exchange.NSE) is None
    assert portfolio.realized_pnl == 1000.0


async def test_bracket_stop_fill_cancels_the_target_leg() -> None:
    manager, _, _ = make_manager()
    entry, _ = await manager.place_order(
        market_request(quantity=10, stop_loss_price=1450.0, target_price=1600.0),
        current_price=1500.0,
        now=_T0,
    )
    all_orders = await manager.get_all_orders()
    target_leg = next(
        o
        for o in all_orders
        if o.parent_order_id == entry.order_id and o.order_type is PaperOrderType.LIMIT
    )

    await manager.process_price_update("INFY-EQ", Exchange.NSE, 1440.0, now=_later(5))

    updated_target = await manager.get_order(target_leg.order_id)
    assert updated_target is not None
    assert updated_target.status == OrderStatus.CANCELLED


async def test_bracket_leg_inconsistent_with_market_fill_price_is_skipped() -> None:
    """A MARKET entry's fill price isn't known until match time — if the
    requested stop_loss/target ends up on the wrong side of where it
    actually filled, that leg is skipped rather than created invalidly
    (see `app.paper.dto.PaperOrderRequest`'s own docstring caveat).
    """

    manager, _, _ = make_manager()

    # stop_loss_price (1550) is ABOVE where a BUY MARKET order will
    # actually fill (1500) -- invalid for a long's stop-loss.
    entry, _ = await manager.place_order(
        market_request(quantity=10, stop_loss_price=1550.0, target_price=1600.0),
        current_price=1500.0,
        now=_T0,
    )

    assert entry.status_message is not None
    assert "stop_loss_price" in entry.status_message
    all_orders = await manager.get_all_orders()
    children = [o for o in all_orders if o.parent_order_id == entry.order_id]
    assert len(children) == 1
    assert children[0].order_type is PaperOrderType.LIMIT  # only the valid target leg


async def test_bracket_on_short_entry_reserves_cash_for_buy_side_legs() -> None:
    manager, portfolio, _ = make_manager()

    entry, _ = await manager.place_order(
        market_request(
            side=OrderSide.SELL, quantity=10, stop_loss_price=1550.0, target_price=1400.0
        ),
        current_price=1500.0,
        now=_T0,
    )
    assert entry.status == OrderStatus.COMPLETE
    cash_after_entry = portfolio.cash

    all_orders = await manager.get_all_orders()
    children = [o for o in all_orders if o.parent_order_id == entry.order_id]
    assert len(children) == 2
    for child in children:
        assert child.side == OrderSide.BUY  # covering a short exits via BUY
    # Both BUY children reserve cash; cash itself (not just available) is untouched.
    assert portfolio.cash == cash_after_entry
    assert portfolio.available_cash < portfolio.cash


# -- FIFO across two entries via the order manager -------------------------------------------------------


async def test_close_spanning_two_entries_produces_two_closed_trades() -> None:
    manager, portfolio, _ = make_manager()
    await manager.place_order(market_request(quantity=5), current_price=1400.0, now=_T0)
    await manager.place_order(market_request(quantity=5), current_price=1600.0, now=_later(1))

    _, trades = await manager.place_order(
        market_request(side=OrderSide.SELL, quantity=7), current_price=1500.0, now=_later(2)
    )

    assert len(trades) == 2
    assert trades[0].entry_price == 1400.0
    assert trades[0].quantity == 5
    assert trades[1].entry_price == 1600.0
    assert trades[1].quantity == 2
    assert portfolio.realized_pnl == trades[0].pnl + trades[1].pnl


# -- lookups / reset -------------------------------------------------------------------------------------


async def test_get_order_for_unknown_id_returns_none() -> None:
    manager, _, _ = make_manager()

    assert await manager.get_order("nope") is None


async def test_get_all_orders_returns_every_placed_order() -> None:
    manager, _, _ = make_manager()
    await manager.place_order(market_request(), current_price=1500.0, now=_T0)
    await manager.place_order(limit_request(price=1400.0), current_price=1500.0, now=_T0)

    orders = await manager.get_all_orders()

    assert len(orders) == 2


async def test_reset_clears_orders_and_reservations() -> None:
    manager, portfolio, _ = make_manager()
    await manager.place_order(limit_request(price=1400.0), current_price=1500.0, now=_T0)
    assert portfolio.available_cash < portfolio.cash

    await manager.reset()

    assert await manager.get_all_orders() == []
    # Reservations aren't portfolio.reset()'s job -- OrderManager.reset()
    # only clears its own order/reservation bookkeeping.
