"""Unit tests for `app.backtest.options_replay.run_options_replay`.

Narrow, deliberately: `option_chain_service`/`option_dataset_provider`
are hand-rolled fakes that raise if ever called, since a flat (never
trending) underlying never produces a qualifying BUY/SELL signal and so
never resolves an option contract at all -- these tests exercise only
the walk-forward signal-generation loop (the same O(n^2) -> O(n)
precomputed-indicator optimization `app.backtest.replay_engine` applies,
re-implemented here per this module's own "not imported" convention),
not the option-chain resolution path.
"""

from typing import Never

import pytest

from app.backtest.costs import TransactionCostModel
from app.backtest.options_replay import run_options_replay
from app.domain.enums.trading import Exchange, HistoricalInterval
from app.options.models import ExpiryMode, StrikeMode
from tests.unit.backtest.conftest import EXCHANGE, INTERVAL, make_flat_candles

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


class _UnusedOptionChainService:
    """Stands in for `OptionChainService` -- must never actually be
    called, since a flat underlying never qualifies for an entry.
    """

    async def get_option_chain(self, underlying: str) -> Never:
        raise AssertionError("option_chain_service must not be called for a flat (HOLD-only) session")


class _UnusedOptionDatasetProvider:
    async def get_candles(self, *args: object, **kwargs: object) -> Never:
        raise AssertionError("option_dataset_provider must not be called for a flat (HOLD-only) session")


async def _run(candles, **overrides):
    kwargs = dict(
        underlying="NIFTY",
        underlying_exchange=EXCHANGE,
        interval=INTERVAL,
        option_chain_service=_UnusedOptionChainService(),
        strike_mode=StrikeMode.ATM,
        expiry_mode=ExpiryMode.NEAREST_WEEKLY,
        option_dataset_provider=_UnusedOptionDatasetProvider(),
        initial_capital=50_000.0,
        confidence_threshold=0.0,
        capital_fraction_per_trade=1.0,
        cost_model=_ZERO_COST,
    )
    kwargs.update(overrides)
    return await run_options_replay(candles, **kwargs)


@pytest.mark.asyncio
async def test_flat_market_never_produces_a_trade() -> None:
    candles = make_flat_candles(60)

    trades, signal_log, _, _ = await _run(candles)

    assert trades == []
    assert len(signal_log) == len(candles)
    assert all(entry.action == "HOLD" for entry in signal_log)


@pytest.mark.asyncio
async def test_no_look_ahead_bias_earlier_signals_are_unaffected_by_later_candles() -> None:
    """Regression test mirroring `test_replay_engine.py`'s own: replaying
    only the first half of a session must produce IDENTICAL signals for
    that half as replaying the full session -- guards the precomputed-
    indicators optimization's core assumption that no later candle ever
    influences an earlier one.
    """

    full_candles = make_flat_candles(80)
    first_half = full_candles[:40]

    _, full_signal_log, _, _ = await _run(full_candles)
    _, half_signal_log, _, _ = await _run(first_half)

    assert full_signal_log[:40] == half_signal_log
