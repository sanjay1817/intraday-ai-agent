"""Unit tests for `app.backtest.costs`."""

from app.backtest.costs import TransactionCostModel, apply_slippage, charges_for_fill
from app.domain.enums.trading import OrderSide


def test_slippage_makes_buys_fill_higher_and_sells_fill_lower() -> None:
    cost_model = TransactionCostModel(slippage_percent=1.0)

    buy_fill = apply_slippage(100.0, OrderSide.BUY, cost_model)
    sell_fill = apply_slippage(100.0, OrderSide.SELL, cost_model)

    assert buy_fill == 101.0
    assert sell_fill == 99.0


def test_zero_slippage_leaves_price_unchanged() -> None:
    cost_model = TransactionCostModel(slippage_percent=0.0)

    assert apply_slippage(250.0, OrderSide.BUY, cost_model) == 250.0
    assert apply_slippage(250.0, OrderSide.SELL, cost_model) == 250.0


def test_stt_only_applies_on_sell_leg() -> None:
    cost_model = TransactionCostModel(
        brokerage_percent=0,
        brokerage_max_per_order=0,
        exchange_txn_charge_percent=0,
        sebi_charges_percent=0,
        stamp_duty_percent=0,
        gst_percent=0,
        stt_percent=0.025,
    )

    buy_charges = charges_for_fill(100.0, 10, OrderSide.BUY, cost_model)
    sell_charges = charges_for_fill(100.0, 10, OrderSide.SELL, cost_model)

    assert buy_charges == 0.0
    assert sell_charges == 1000.0 * (0.025 / 100.0)


def test_stamp_duty_only_applies_on_buy_leg() -> None:
    cost_model = TransactionCostModel(
        brokerage_percent=0,
        brokerage_max_per_order=0,
        exchange_txn_charge_percent=0,
        sebi_charges_percent=0,
        stt_percent=0,
        gst_percent=0,
        stamp_duty_percent=0.003,
    )

    buy_charges = charges_for_fill(100.0, 10, OrderSide.BUY, cost_model)
    sell_charges = charges_for_fill(100.0, 10, OrderSide.SELL, cost_model)

    assert buy_charges == 1000.0 * (0.003 / 100.0)
    assert sell_charges == 0.0


def test_brokerage_is_capped_at_max_per_order() -> None:
    cost_model = TransactionCostModel(
        brokerage_percent=100.0,  # deliberately absurd to force the cap
        brokerage_max_per_order=20.0,
        exchange_txn_charge_percent=0,
        sebi_charges_percent=0,
        stt_percent=0,
        gst_percent=0,
        stamp_duty_percent=0,
    )

    charges = charges_for_fill(100.0, 10, OrderSide.BUY, cost_model)

    assert charges == 20.0


def test_charges_are_deterministic_for_the_same_inputs() -> None:
    cost_model = TransactionCostModel()

    first = charges_for_fill(523.45, 17, OrderSide.SELL, cost_model)
    second = charges_for_fill(523.45, 17, OrderSide.SELL, cost_model)

    assert first == second
    assert first > 0
