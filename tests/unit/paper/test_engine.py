"""Unit tests for `app.paper.engine.PaperTradingEngine`.

The underlying managers (`PortfolioManager`/`PositionManager`/
`OrderManager`) already have their own thorough test suites — these
tests focus on wiring: does the facade delegate correctly, accumulate
trade history across calls, and reset everything together.
"""

from datetime import UTC, datetime

import pytest

from app.domain.enums.trading import Exchange, OrderSide
from app.domain.exceptions.paper import OrderNotFoundError
from app.paper.dto import OrderModificationRequest, PaperOrderRequest, PaperOrderType
from app.paper.engine import PaperTradingEngine
from app.paper.models import TradeMetadata

_T0 = datetime(2024, 1, 1, 9, 15, tzinfo=UTC)


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
    symbol: str = "INFY-EQ", price: float = 1400.0, quantity: int = 10, **overrides: object
) -> PaperOrderRequest:
    defaults: dict[str, object] = {
        "symbol": symbol,
        "exchange": Exchange.NSE,
        "side": OrderSide.BUY,
        "order_type": PaperOrderType.LIMIT,
        "quantity": quantity,
        "price": price,
    }
    defaults.update(overrides)
    return PaperOrderRequest(**defaults)  # type: ignore[arg-type]


# -- construction / defaults ----------------------------------------------------------------


async def test_default_initial_capital() -> None:
    engine = PaperTradingEngine()

    portfolio = await engine.get_portfolio()

    assert portfolio.cash == 100_000.0
    assert portfolio.initial_capital == 100_000.0


async def test_custom_initial_capital() -> None:
    engine = PaperTradingEngine(initial_capital=500_000.0)

    portfolio = await engine.get_portfolio()

    assert portfolio.cash == 500_000.0


# -- place_order / trade history -----------------------------------------------------------------


async def test_place_order_returns_the_order_and_updates_portfolio() -> None:
    engine = PaperTradingEngine()

    order = await engine.place_order(market_request(), current_price=1500.0, now=_T0)

    assert order.status.value == "COMPLETE"
    portfolio = await engine.get_portfolio()
    assert portfolio.cash == 100_000.0 - 15_000.0


async def test_place_order_with_metadata_is_carried_onto_a_closed_trade() -> None:
    engine = PaperTradingEngine()
    metadata = TradeMetadata(confidence=88.0, agreeing_strategies=["ema_trend"])
    await engine.place_order(market_request(), current_price=1500.0, metadata=metadata, now=_T0)

    await engine.place_order(market_request(side=OrderSide.SELL), current_price=1600.0, now=_T0)

    trades = await engine.get_trades()
    assert len(trades) == 1
    assert trades[0].metadata is not None
    assert trades[0].metadata.confidence == 88.0


async def test_trade_history_accumulates_across_multiple_closes() -> None:
    engine = PaperTradingEngine()
    await engine.place_order(market_request(quantity=10), current_price=1500.0, now=_T0)
    await engine.place_order(
        market_request(side=OrderSide.SELL, quantity=4), current_price=1550.0, now=_T0
    )
    await engine.place_order(
        market_request(side=OrderSide.SELL, quantity=6), current_price=1600.0, now=_T0
    )

    trades = await engine.get_trades()

    assert len(trades) == 2
    assert trades[0].quantity == 4
    assert trades[1].quantity == 6


# -- update_price ---------------------------------------------------------------------------------


async def test_update_price_fills_a_resting_order() -> None:
    engine = PaperTradingEngine()
    order = await engine.place_order(limit_request(price=1400.0), current_price=1500.0, now=_T0)
    assert order.status.value == "OPEN"

    await engine.update_price("INFY-EQ", Exchange.NSE, 1390.0, now=_T0)

    updated = await engine.get_order(order.order_id)
    assert updated is not None
    assert updated.status.value == "COMPLETE"


async def test_update_price_marks_open_positions_with_no_resting_orders() -> None:
    engine = PaperTradingEngine()
    await engine.place_order(market_request(quantity=10), current_price=1500.0, now=_T0)

    await engine.update_price("INFY-EQ", Exchange.NSE, 1600.0, now=_T0)

    position = await engine.get_position("INFY-EQ", Exchange.NSE)
    assert position is not None
    assert position.last_price == 1600.0
    assert position.unrealized_pnl == 10 * (1600.0 - 1500.0)


async def test_update_price_returns_trades_produced_by_filled_orders() -> None:
    engine = PaperTradingEngine()
    await engine.place_order(market_request(quantity=10), current_price=1500.0, now=_T0)
    await engine.place_order(
        limit_request(price=1600.0, quantity=10, side=OrderSide.SELL), current_price=1500.0, now=_T0
    )

    # SELL LIMIT @ 1600 becomes marketable once price reaches 1650, and
    # fills AT that price improvement (1650), not at the limit itself.
    trades = await engine.update_price("INFY-EQ", Exchange.NSE, 1650.0, now=_T0)

    assert len(trades) == 1
    assert trades[0].pnl == 10 * (1650.0 - 1500.0)
    all_trades = await engine.get_trades()
    assert all_trades == trades


# -- cancel / modify delegate correctly --------------------------------------------------------------


async def test_cancel_order_delegates_to_order_manager() -> None:
    engine = PaperTradingEngine()
    order = await engine.place_order(limit_request(), current_price=1500.0, now=_T0)

    cancelled = await engine.cancel_order(order.order_id, now=_T0)

    assert cancelled.status.value == "CANCELLED"


async def test_cancel_unknown_order_propagates_domain_exception() -> None:
    engine = PaperTradingEngine()

    with pytest.raises(OrderNotFoundError):
        await engine.cancel_order("no-such-id")


async def test_modify_order_delegates_to_order_manager() -> None:
    engine = PaperTradingEngine()
    order = await engine.place_order(limit_request(quantity=10), current_price=1500.0, now=_T0)

    updated = await engine.modify_order(
        OrderModificationRequest(order_id=order.order_id, quantity=20), now=_T0
    )

    assert updated.quantity == 20


# -- lookups ----------------------------------------------------------------------------------------


async def test_get_orders_lists_every_placed_order() -> None:
    engine = PaperTradingEngine()
    await engine.place_order(market_request(), current_price=1500.0, now=_T0)
    await engine.place_order(limit_request(), current_price=1500.0, now=_T0)

    orders = await engine.get_orders()

    assert len(orders) == 2


async def test_get_order_for_unknown_id_returns_none() -> None:
    engine = PaperTradingEngine()

    assert await engine.get_order("nope") is None


async def test_get_positions_lists_every_open_position() -> None:
    engine = PaperTradingEngine()
    await engine.place_order(market_request(symbol="INFY-EQ"), current_price=1500.0, now=_T0)
    await engine.place_order(market_request(symbol="TCS-EQ"), current_price=3500.0, now=_T0)

    positions = await engine.get_positions()

    assert {p.symbol for p in positions} == {"INFY-EQ", "TCS-EQ"}


async def test_get_position_for_flat_symbol_returns_none() -> None:
    engine = PaperTradingEngine()

    assert await engine.get_position("INFY-EQ", Exchange.NSE) is None


async def test_get_trades_empty_initially() -> None:
    engine = PaperTradingEngine()

    assert await engine.get_trades() == []


# -- reset --------------------------------------------------------------------------------------------


async def test_reset_clears_orders_positions_and_trade_history() -> None:
    engine = PaperTradingEngine()
    await engine.place_order(market_request(quantity=10), current_price=1500.0, now=_T0)
    await engine.place_order(
        market_request(side=OrderSide.SELL, quantity=10), current_price=1600.0, now=_T0
    )
    assert await engine.get_trades() != []

    await engine.reset()

    assert await engine.get_orders() == []
    assert await engine.get_positions() == []
    assert await engine.get_trades() == []
    portfolio = await engine.get_portfolio()
    assert portfolio.cash == 100_000.0
    assert portfolio.realized_pnl == 0.0


async def test_reset_with_new_capital() -> None:
    engine = PaperTradingEngine(initial_capital=100_000.0)

    await engine.reset(initial_capital=250_000.0)

    portfolio = await engine.get_portfolio()
    assert portfolio.cash == 250_000.0
    assert portfolio.initial_capital == 250_000.0


async def test_reset_releases_reserved_cash_from_resting_orders() -> None:
    engine = PaperTradingEngine()
    await engine.place_order(
        limit_request(price=1400.0, quantity=10), current_price=1500.0, now=_T0
    )
    portfolio_before = await engine.get_portfolio()
    assert portfolio_before.available_cash < portfolio_before.cash

    await engine.reset()

    portfolio_after = await engine.get_portfolio()
    assert portfolio_after.available_cash == portfolio_after.cash == 100_000.0
