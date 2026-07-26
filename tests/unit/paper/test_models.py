"""Unit tests for `app.paper.models`."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain.enums.trading import Exchange, OrderSide, OrderStatus
from app.paper.dto import PaperOrderType
from app.paper.models import ClosedTrade, Fill, PaperOrder, PaperPosition, Portfolio, TradeMetadata

_NOW = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
_LATER = datetime(2024, 1, 1, 10, 5, tzinfo=UTC)


def make_order(**overrides: object) -> PaperOrder:
    defaults: dict[str, object] = {
        "order_id": "order-1",
        "symbol": "INFY-EQ",
        "exchange": Exchange.NSE,
        "side": OrderSide.BUY,
        "order_type": PaperOrderType.MARKET,
        "quantity": 10,
        "status": OrderStatus.OPEN,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return PaperOrder(**defaults)  # type: ignore[arg-type]


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


def make_closed_trade(**overrides: object) -> ClosedTrade:
    defaults: dict[str, object] = {
        "trade_id": "trade-1",
        "symbol": "INFY-EQ",
        "exchange": Exchange.NSE,
        "side": OrderSide.BUY,
        "quantity": 10,
        "entry_price": 1500.0,
        "exit_price": 1550.0,
        "pnl": 500.0,
        "entry_order_id": "order-1",
        "exit_order_id": "order-2",
        "entry_timestamp": _NOW,
        "exit_timestamp": _LATER,
    }
    defaults.update(overrides)
    return ClosedTrade(**defaults)  # type: ignore[arg-type]


def make_portfolio(**overrides: object) -> Portfolio:
    defaults: dict[str, object] = {
        "cash": 100_000.0,
        "available_cash": 100_000.0,
        "used_capital": 0.0,
        "initial_capital": 100_000.0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "total_pnl": 0.0,
        "equity": 100_000.0,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return Portfolio(**defaults)  # type: ignore[arg-type]


# -- TradeMetadata --------------------------------------------------------------------


def test_trade_metadata_defaults_are_all_empty() -> None:
    metadata = TradeMetadata()

    assert metadata.confidence is None
    assert metadata.agreeing_strategies == []
    assert metadata.conflicting_strategies == []
    assert metadata.indicators_used == []
    assert metadata.reasoning is None


def test_trade_metadata_confidence_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        TradeMetadata(confidence=101.0)


# -- PaperOrder -------------------------------------------------------------------------


def test_paper_order_constructs_with_defaults() -> None:
    order = make_order()

    assert order.filled_quantity == 0
    assert order.average_fill_price is None
    assert order.metadata is None


def test_paper_order_is_frozen() -> None:
    order = make_order()

    with pytest.raises(ValidationError):
        order.status = OrderStatus.COMPLETE  # type: ignore[misc]


def test_paper_order_filled_quantity_cannot_exceed_quantity() -> None:
    with pytest.raises(ValidationError, match="filled_quantity"):
        make_order(quantity=10, filled_quantity=11)


def test_paper_order_complete_requires_full_fill() -> None:
    with pytest.raises(ValidationError, match="filled_quantity == quantity"):
        make_order(
            quantity=10,
            filled_quantity=5,
            status=OrderStatus.COMPLETE,
            average_fill_price=1500.0,
        )


def test_paper_order_complete_requires_average_fill_price() -> None:
    with pytest.raises(ValidationError, match="average_fill_price"):
        make_order(quantity=10, filled_quantity=10, status=OrderStatus.COMPLETE)


def test_paper_order_complete_with_consistent_fill_state_succeeds() -> None:
    order = make_order(
        quantity=10,
        filled_quantity=10,
        status=OrderStatus.COMPLETE,
        average_fill_price=1502.5,
    )

    assert order.status == OrderStatus.COMPLETE
    assert order.average_fill_price == 1502.5


def test_paper_order_carries_bracket_linkage_fields() -> None:
    order = make_order(parent_order_id="entry-1", oco_group_id="oco-1")

    assert order.parent_order_id == "entry-1"
    assert order.oco_group_id == "oco-1"


def test_paper_order_carries_metadata() -> None:
    metadata = TradeMetadata(confidence=87.5, agreeing_strategies=["ema_trend"])

    order = make_order(metadata=metadata)

    assert order.metadata is not None
    assert order.metadata.confidence == 87.5


# -- Fill -------------------------------------------------------------------------------


def test_fill_constructs_with_required_fields() -> None:
    fill = Fill(
        fill_id="fill-1",
        order_id="order-1",
        symbol="INFY-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=10,
        price=1500.0,
        timestamp=_NOW,
    )

    assert fill.quantity == 10
    assert fill.price == 1500.0


def test_fill_rejects_non_positive_quantity() -> None:
    with pytest.raises(ValidationError):
        Fill(
            fill_id="fill-1",
            order_id="order-1",
            symbol="INFY-EQ",
            exchange=Exchange.NSE,
            side=OrderSide.BUY,
            quantity=0,
            price=1500.0,
            timestamp=_NOW,
        )


# -- PaperPosition ------------------------------------------------------------------------


def test_position_constructs_with_consistent_pnl() -> None:
    position = make_position(quantity=10, average_price=1500.0, last_price=1550.0)

    assert position.unrealized_pnl == 500.0


def test_position_short_quantity_unrealized_pnl() -> None:
    position = make_position(
        quantity=-10, average_price=1550.0, last_price=1500.0, unrealized_pnl=500.0
    )

    assert position.unrealized_pnl == 500.0


def test_position_non_flat_requires_positive_average_price() -> None:
    with pytest.raises(ValidationError, match="average_price"):
        make_position(quantity=10, average_price=0.0, unrealized_pnl=0.0)


def test_position_flat_allows_zero_average_price() -> None:
    position = make_position(quantity=0, average_price=0.0, unrealized_pnl=0.0)

    assert position.quantity == 0


def test_position_unrealized_pnl_mismatch_rejected() -> None:
    with pytest.raises(ValidationError, match="unrealized_pnl"):
        make_position(quantity=10, average_price=1500.0, last_price=1550.0, unrealized_pnl=999.0)


def test_position_is_frozen() -> None:
    position = make_position()

    with pytest.raises(ValidationError):
        position.last_price = 1600.0  # type: ignore[misc]


# -- ClosedTrade --------------------------------------------------------------------------


def test_closed_trade_buy_side_pnl_matches_formula() -> None:
    trade = make_closed_trade(
        side=OrderSide.BUY, entry_price=1500.0, exit_price=1550.0, quantity=10, pnl=500.0
    )

    assert trade.pnl == 500.0


def test_closed_trade_sell_side_pnl_matches_formula() -> None:
    trade = make_closed_trade(
        side=OrderSide.SELL, entry_price=1550.0, exit_price=1500.0, quantity=10, pnl=500.0
    )

    assert trade.pnl == 500.0


def test_closed_trade_pnl_mismatch_rejected() -> None:
    with pytest.raises(ValidationError, match="pnl"):
        make_closed_trade(
            side=OrderSide.BUY, entry_price=1500.0, exit_price=1550.0, quantity=10, pnl=999.0
        )


def test_closed_trade_exit_before_entry_rejected() -> None:
    with pytest.raises(ValidationError, match="exit_timestamp"):
        make_closed_trade(entry_timestamp=_LATER, exit_timestamp=_NOW)


def test_closed_trade_carries_optional_metadata() -> None:
    metadata = TradeMetadata(
        confidence=72.0,
        agreeing_strategies=["ema_trend", "rsi_macd_reversal"],
        indicators_used=["EMA", "RSI", "MACD"],
        reasoning="BUY: 2 of 3 strategies agree.",
    )

    trade = make_closed_trade(metadata=metadata)

    assert trade.metadata is not None
    assert trade.metadata.agreeing_strategies == ["ema_trend", "rsi_macd_reversal"]


def test_closed_trade_metadata_defaults_to_none() -> None:
    trade = make_closed_trade()

    assert trade.metadata is None


# -- Portfolio ----------------------------------------------------------------------------


def test_portfolio_with_no_positions_is_flat() -> None:
    portfolio = make_portfolio(cash=100_000.0, available_cash=100_000.0, equity=100_000.0)

    assert portfolio.positions == []
    assert portfolio.equity == 100_000.0
    assert portfolio.used_capital == 0.0


def test_portfolio_equity_matches_cash_plus_positions_market_value() -> None:
    position = make_position(quantity=10, average_price=1500.0, last_price=1550.0)
    portfolio = make_portfolio(
        cash=85_000.0,
        available_cash=85_000.0,
        used_capital=15_000.0,
        unrealized_pnl=500.0,
        total_pnl=500.0,
        equity=100_500.0,
        positions=[position],
    )

    assert portfolio.equity == 100_500.0


def test_portfolio_equity_mismatch_rejected() -> None:
    position = make_position(quantity=10, average_price=1500.0, last_price=1550.0)

    with pytest.raises(ValidationError, match="equity"):
        make_portfolio(
            cash=85_000.0,
            available_cash=85_000.0,
            used_capital=15_000.0,
            unrealized_pnl=500.0,
            total_pnl=500.0,
            equity=999_999.0,
            positions=[position],
        )


def test_portfolio_unrealized_pnl_mismatch_rejected() -> None:
    position = make_position(quantity=10, average_price=1500.0, last_price=1550.0)

    with pytest.raises(ValidationError, match="unrealized_pnl"):
        make_portfolio(
            cash=85_000.0,
            available_cash=85_000.0,
            used_capital=15_000.0,
            unrealized_pnl=1234.0,
            total_pnl=1234.0,
            equity=100_500.0,
            positions=[position],
        )


def test_portfolio_used_capital_matches_positions_regardless_of_direction() -> None:
    """`used_capital` sums cost basis by magnitude — a short position ties
    up capital just as much as a long one of the same size.
    """

    long_position = make_position(
        symbol="INFY-EQ", quantity=10, average_price=1500.0, last_price=1550.0
    )
    short_position = make_position(
        symbol="TCS-EQ",
        quantity=-5,
        average_price=3500.0,
        last_price=3400.0,
        unrealized_pnl=500.0,
    )
    portfolio = make_portfolio(
        cash=70_000.0,
        available_cash=70_000.0,
        used_capital=15_000.0 + 17_500.0,  # 10*1500 + 5*3500
        unrealized_pnl=1000.0,
        total_pnl=1000.0,
        # 70_000 cash + (10 * 1550) + (-5 * 3400) market value
        equity=70_000.0 + 15_500.0 - 17_000.0,
        positions=[long_position, short_position],
    )

    assert portfolio.used_capital == 32_500.0
    assert portfolio.unrealized_pnl == 1000.0
    assert len(portfolio.positions) == 2


def test_portfolio_used_capital_mismatch_rejected() -> None:
    position = make_position(quantity=10, average_price=1500.0, last_price=1550.0)

    with pytest.raises(ValidationError, match="used_capital"):
        make_portfolio(
            cash=85_000.0,
            available_cash=85_000.0,
            used_capital=999.0,
            unrealized_pnl=500.0,
            total_pnl=500.0,
            equity=100_500.0,
            positions=[position],
        )


def test_portfolio_available_cash_cannot_exceed_cash() -> None:
    with pytest.raises(ValidationError, match="available_cash"):
        make_portfolio(cash=50_000.0, available_cash=60_000.0)


def test_portfolio_available_cash_can_be_less_than_cash_when_reserved() -> None:
    """Cash reserved against a resting order reduces `available_cash`
    without touching `cash` itself.
    """

    portfolio = make_portfolio(cash=100_000.0, available_cash=85_000.0)

    assert portfolio.available_cash == 85_000.0
    assert portfolio.cash == 100_000.0


def test_portfolio_total_pnl_matches_realized_plus_unrealized() -> None:
    position = make_position(
        quantity=10, average_price=1500.0, last_price=1520.0, unrealized_pnl=200.0
    )
    portfolio = make_portfolio(
        cash=85_000.0,
        available_cash=85_000.0,
        used_capital=15_000.0,
        realized_pnl=300.0,
        unrealized_pnl=200.0,
        total_pnl=500.0,
        equity=85_000.0 + 10 * 1520.0,
        positions=[position],
    )

    assert portfolio.total_pnl == 500.0


def test_portfolio_total_pnl_mismatch_rejected() -> None:
    position = make_position(
        quantity=10, average_price=1500.0, last_price=1520.0, unrealized_pnl=200.0
    )

    with pytest.raises(ValidationError, match="total_pnl"):
        make_portfolio(
            cash=85_000.0,
            available_cash=85_000.0,
            used_capital=15_000.0,
            realized_pnl=300.0,
            unrealized_pnl=200.0,
            total_pnl=999.0,
            equity=85_000.0 + 10 * 1520.0,
            positions=[position],
        )


def test_portfolio_total_pnl_reflects_a_loss() -> None:
    portfolio = make_portfolio(
        cash=90_000.0,
        available_cash=90_000.0,
        realized_pnl=-1000.0,
        unrealized_pnl=0.0,
        total_pnl=-1000.0,
        equity=90_000.0,
    )

    assert portfolio.total_pnl == -1000.0


def test_portfolio_is_frozen() -> None:
    portfolio = make_portfolio()

    with pytest.raises(ValidationError):
        portfolio.cash = 0.0  # type: ignore[misc]
