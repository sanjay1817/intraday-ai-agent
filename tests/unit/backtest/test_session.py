"""Unit tests for `app.backtest.session.BacktestOrchestrator`: date
validation, single-run persistence, and multi-date aggregate stats.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.backtest.dto import BacktestRequest
from app.backtest.session import BacktestOrchestrator
from app.domain.entities.broker import HistoricalBar
from app.domain.enums.trading import BrokerName, Exchange, HistoricalInterval
from app.domain.exceptions.backtest import FutureDateError
from app.domain.exceptions.market import NoHistoricalDataError

_FROM = datetime(2026, 8, 5, 9, 15, tzinfo=UTC)


class _StubBroker:
    def __init__(self, bars: list[HistoricalBar]) -> None:
        self._bars = bars
        self.calls: list[tuple[str, datetime, datetime]] = []

    async def historical_data(
        self,
        exchange: Exchange,
        tradingsymbol: str,
        interval: HistoricalInterval,
        from_date: datetime,
        to_date: datetime,
    ) -> list[HistoricalBar]:
        self.calls.append((tradingsymbol, from_date, to_date))
        return self._bars


def _flat_bars(n: int) -> list[HistoricalBar]:
    return [
        HistoricalBar(
            timestamp=_FROM + timedelta(minutes=i),
            open=100.0,
            high=100.1,
            low=99.9,
            close=100.0,
            volume=1_000,
        )
        for i in range(n)
    ]


def _request(historical_date=None, symbol="RELIANCE-EQ") -> BacktestRequest:
    return BacktestRequest(
        symbol=symbol,
        exchange=Exchange.NSE,
        historical_date=historical_date or _FROM.date(),
        initial_capital=50_000.0,
        interval=HistoricalInterval.ONE_MINUTE,
    )


@pytest.mark.asyncio
async def test_run_single_rejects_a_future_dated_request(tmp_path: Path) -> None:
    broker = _StubBroker(_flat_bars(60))
    orchestrator = BacktestOrchestrator(broker, BrokerName.ANGEL_ONE, data_dir=tmp_path)  # type: ignore[arg-type]

    request = _request(historical_date=_FROM.date())

    with pytest.raises(FutureDateError):
        await orchestrator.run_single(request, today=_FROM.date())

    assert broker.calls == []  # rejected before any broker call was made


@pytest.mark.asyncio
async def test_run_single_raises_no_historical_data_for_an_empty_broker_response(
    tmp_path: Path,
) -> None:
    broker = _StubBroker([])
    orchestrator = BacktestOrchestrator(broker, BrokerName.ANGEL_ONE, data_dir=tmp_path)  # type: ignore[arg-type]

    request = _request()

    with pytest.raises(NoHistoricalDataError):
        await orchestrator.run_single(request, today=_FROM.date() + timedelta(days=1))


@pytest.mark.asyncio
async def test_run_single_persists_the_result_and_it_can_be_reloaded(tmp_path: Path) -> None:
    broker = _StubBroker(_flat_bars(60))
    orchestrator = BacktestOrchestrator(broker, BrokerName.ANGEL_ONE, data_dir=tmp_path)  # type: ignore[arg-type]

    result = await orchestrator.run_single(
        _request(), today=_FROM.date() + timedelta(days=1)
    )
    reloaded = orchestrator.load_run(result.run_id)

    assert reloaded is not None
    assert reloaded.run_id == result.run_id
    assert reloaded.summary.initial_capital == 50_000.0
    assert result.run_id in orchestrator.list_run_ids()


@pytest.mark.asyncio
async def test_run_batch_aggregates_across_independent_sessions(tmp_path: Path) -> None:
    broker = _StubBroker(_flat_bars(60))
    orchestrator = BacktestOrchestrator(broker, BrokerName.ANGEL_ONE, data_dir=tmp_path)  # type: ignore[arg-type]

    requests = [
        _request(historical_date=_FROM.date()),
        _request(historical_date=_FROM.date() + timedelta(days=1)),
        _request(historical_date=_FROM.date() + timedelta(days=2)),
    ]

    aggregate = await orchestrator.run_batch(requests, today=_FROM.date() + timedelta(days=10))

    assert aggregate.aggregate.total_sessions == 3
    assert len(aggregate.session_results) == 3
    # A flat market never trades -- both session-level and aggregate P&L
    # must be exactly zero, a simple determinism check on the rollup math.
    assert aggregate.aggregate.total_pnl == 0.0
    assert aggregate.aggregate.total_trades == 0
