"""Transaction Cost Model: configurable brokerage/charges/slippage
assumptions applied to simulated fills during a historical replay.

`app.paper.engine.PaperTradingEngine` fills every order at exactly the
caller-supplied price with zero charges — correct for live/manual paper
trading, where nothing here should change its behavior. A historical
backtest, on the other hand, explicitly asked (see the feature's
requirements) for configurable brokerage/taxes/slippage so its P&L isn't
misleadingly optimistic. Rather than adding charges to the shared
`PaperTradingEngine`/`ClosedTrade` models (which would ripple into every
existing paper/auto-trading consumer), this module computes the
slippage-adjusted execution price *before* calling `place_order`, and
computes charges as a separate, reportable number the backtest layer
subtracts from `ClosedTrade.pnl` to get `BacktestTradeRecord.net_pnl`.

Defaults approximate NSE equity intraday charges (brokerage, STT,
exchange transaction charges, SEBI charges, stamp duty, GST) as of
general public rate schedules — they are a configurable estimate, not a
guarantee of what a live broker would actually charge.
"""

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums.trading import OrderSide


class TransactionCostModel(BaseModel):
    """One backtest run's cost assumptions. Every field is overridable
    per-request; the defaults are a reasonable NSE equity intraday
    estimate.
    """

    model_config = ConfigDict(frozen=True)

    #: Brokerage per executed order: the lesser of `brokerage_percent *
    #: turnover` and `brokerage_max_per_order` (many discount brokers
    #: price intraday equity this way — e.g. 0.03% or ₹20, whichever is
    #: lower).
    brokerage_percent: float = Field(default=0.03, ge=0)
    brokerage_max_per_order: float = Field(default=20.0, ge=0)

    #: Securities Transaction Tax — intraday equity charges STT on the
    #: sell leg only.
    stt_percent: float = Field(default=0.025, ge=0)

    #: NSE exchange transaction charges, both legs.
    exchange_txn_charge_percent: float = Field(default=0.00297, ge=0)

    #: SEBI turnover charges, both legs.
    sebi_charges_percent: float = Field(default=0.0001, ge=0)

    #: Stamp duty, buy leg only.
    stamp_duty_percent: float = Field(default=0.003, ge=0)

    #: GST on (brokerage + exchange transaction charges).
    gst_percent: float = Field(default=18.0, ge=0)

    #: Slippage applied to every simulated fill: the price actually
    #: filled is worse than the signal price by this percentage, in the
    #: direction that hurts the trade (buys fill higher, sells fill
    #: lower) — a simple, transparent stand-in for bid/ask spread and
    #: market impact, not a liquidity/order-book simulation.
    slippage_percent: float = Field(default=0.05, ge=0)


def apply_slippage(price: float, side: OrderSide, cost_model: TransactionCostModel) -> float:
    """The execution price after slippage: worse than `price` for the
    trader, in `side`'s direction — a BUY fills higher, a SELL fills
    lower.
    """

    adjustment = price * (cost_model.slippage_percent / 100.0)
    return price + adjustment if side is OrderSide.BUY else price - adjustment


def charges_for_fill(
    price: float, quantity: int, side: OrderSide, cost_model: TransactionCostModel
) -> float:
    """Total statutory + brokerage charges for one executed fill
    (one leg of a round-trip trade).
    """

    turnover = price * quantity

    brokerage = min(
        turnover * (cost_model.brokerage_percent / 100.0), cost_model.brokerage_max_per_order
    )
    exchange_txn_charge = turnover * (cost_model.exchange_txn_charge_percent / 100.0)
    sebi_charges = turnover * (cost_model.sebi_charges_percent / 100.0)
    gst = (brokerage + exchange_txn_charge) * (cost_model.gst_percent / 100.0)

    stt = turnover * (cost_model.stt_percent / 100.0) if side is OrderSide.SELL else 0.0
    stamp_duty = turnover * (cost_model.stamp_duty_percent / 100.0) if side is OrderSide.BUY else 0.0

    return brokerage + exchange_txn_charge + sebi_charges + gst + stt + stamp_duty
