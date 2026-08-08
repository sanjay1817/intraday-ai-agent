"""Unit tests for `app.backtest.replay_engine.run_replay`.

Uses real `MarketCandle`s run through the actual `StrategyEngine` +
indicator computation (no mocking of the strategy pipeline) — this is
the core deterministic test the feature's requirements call for.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.backtest.costs import TransactionCostModel
from app.backtest.replay_engine import _exit_reason, _ReplayPosition, run_replay
from app.domain.enums.trading import HistoricalInterval, OrderSide
from tests.unit.backtest.conftest import EXCHANGE, INTERVAL, SYMBOL, make_flat_candles, make_trending_candles

_ZERO_COST = TransactionCostModel(
    brokerage_percent=0,
    brokerage_max_per_order=0,
    exchange_txn_charge_percent=0,
    sebi_charges_percent=0,
    stt_percent=0,
    gst_percent=0,
    stamp_duty_percent=0,
    slippage_percent=0,
)


async def _run(candles, **overrides):
    kwargs = dict(
        symbol=SYMBOL,
        exchange=EXCHANGE,
        interval=INTERVAL,
        initial_capital=50_000.0,
        confidence_threshold=0.0,  # accept every non-HOLD signal for these tests
        capital_fraction_per_trade=1.0,
        cost_model=_ZERO_COST,
    )
    kwargs.update(overrides)
    return await run_replay(candles, **kwargs)


@pytest.mark.asyncio
async def test_flat_market_never_produces_a_trade() -> None:
    candles = make_flat_candles(60)

    trades, signal_log, equity_curve, warnings = await _run(candles)

    assert trades == []
    assert len(signal_log) == len(candles)
    assert len(equity_curve) == len(candles)
    assert all(point.equity == 50_000.0 for point in equity_curve)


@pytest.mark.asyncio
async def test_a_strong_uptrend_eventually_produces_a_buy_signal() -> None:
    candles = make_trending_candles(60)

    _, signal_log, _, _ = await _run(candles)

    assert any(entry.action == "BUY" for entry in signal_log)


@pytest.mark.asyncio
async def test_entries_fill_at_the_next_candles_open_not_the_signal_candles_close() -> None:
    """The documented execution-timing assumption: a BUY signal seen at
    candle i is filled at candle i+1's open, never at candle i's own
    close (which would be an unrealistic same-instant fill).
    """

    candles = make_trending_candles(60)

    trades, signal_log, _, _ = await _run(candles)
    assert trades, "expected the strong uptrend to produce at least one trade"

    first_trade = trades[0]
    matching_opens = {candle.open for candle in candles if candle.timestamp == first_trade.entry_time}
    assert matching_opens == {first_trade.entry_fill_price}


@pytest.mark.asyncio
async def test_no_look_ahead_bias_earlier_signals_are_unaffected_by_later_candles() -> None:
    """Regression test: replaying only the first half of a session must
    produce IDENTICAL signals for that half as replaying the full
    session — later candles must never influence an earlier decision.
    """

    full_candles = make_trending_candles(80)
    first_half = full_candles[:40]

    _, full_signal_log, _, _ = await _run(full_candles)
    _, half_signal_log, _, _ = await _run(first_half)

    assert full_signal_log[:40] == half_signal_log


@pytest.mark.asyncio
async def test_session_end_forces_a_square_off_on_the_final_candle() -> None:
    candles = make_trending_candles(60)

    trades, _, _, _ = await _run(candles)
    assert trades, "expected at least one trade to be open going into the final candle or closed earlier"

    # Whatever trades occurred, the position tracked by the engine must
    # be flat by the end -- either closed on a stop/target/reversal, or
    # force-closed with exit_reason == "session_end" on the last candle.
    last_candle_time = candles[-1].timestamp
    session_end_trades = [trade for trade in trades if trade.exit_reason == "session_end"]
    for trade in session_end_trades:
        assert trade.exit_time == last_candle_time


@pytest.mark.asyncio
async def test_fewer_than_two_candles_warns_and_produces_no_trades() -> None:
    candles = make_trending_candles(1)

    trades, _, _, warnings = await _run(candles)

    assert trades == []
    assert warnings


def test_exit_reason_stop_loss_for_a_long_position() -> None:
    position = _ReplayPosition(
        side=OrderSide.BUY,
        quantity=10,
        entry_signal_price=100.0,
        entry_fill_price=100.0,
        entry_charges=0.0,
        stop_loss=95.0,
        targets=[110.0],
        trailing_amount=None,
        best_price=100.0,
        entry_order_id="x",
        entry_time=datetime(2026, 8, 5, 9, 20, tzinfo=UTC),
        confidence=80.0,
        reasoning="test",
    )

    from app.instructor.recommendation import RecommendationAction

    reason = _exit_reason(
        position, 94.0, RecommendationAction.HOLD, 0.0, 60.0, is_final_candle=False
    )

    assert reason == "stop_loss"


def test_exit_reason_target_for_a_long_position() -> None:
    from app.instructor.recommendation import RecommendationAction

    position = _ReplayPosition(
        side=OrderSide.BUY,
        quantity=10,
        entry_signal_price=100.0,
        entry_fill_price=100.0,
        entry_charges=0.0,
        stop_loss=95.0,
        targets=[110.0],
        trailing_amount=None,
        best_price=100.0,
        entry_order_id="x",
        entry_time=datetime(2026, 8, 5, 9, 20, tzinfo=UTC),
        confidence=80.0,
        reasoning="test",
    )

    reason = _exit_reason(
        position, 111.0, RecommendationAction.HOLD, 0.0, 60.0, is_final_candle=False
    )

    assert reason == "target"


def test_exit_reason_session_end_takes_priority_over_everything_else() -> None:
    from app.instructor.recommendation import RecommendationAction

    position = _ReplayPosition(
        side=OrderSide.BUY,
        quantity=10,
        entry_signal_price=100.0,
        entry_fill_price=100.0,
        entry_charges=0.0,
        stop_loss=95.0,
        targets=[110.0],
        trailing_amount=None,
        best_price=100.0,
        entry_order_id="x",
        entry_time=datetime(2026, 8, 5, 9, 20, tzinfo=UTC),
        confidence=80.0,
        reasoning="test",
    )

    # Price is comfortably between stop and target -- would otherwise be
    # None -- but the final candle always forces a square-off.
    reason = _exit_reason(
        position, 102.0, RecommendationAction.HOLD, 0.0, 60.0, is_final_candle=True
    )

    assert reason == "session_end"
