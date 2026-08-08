"""The Historical Backtesting API: replay a past trading session against
the existing strategy/paper-execution pipeline.

Every response is a `HISTORICAL BACKTEST` result (see
`app.backtest.dto.BACKTEST_DISCLAIMER`) — never live trading, never
manual/automatic paper trading. `app.backtest.session.BacktestOrchestrator`
holds only a read-only broker (historical data only) and a fresh,
isolated `PaperTradingEngine` per run; this router never touches
`app.state.paper_engine` or any live order-placement path.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.backtest.dto import (
    AggregateBacktestResult,
    BacktestRequest,
    BacktestResult,
    BacktestTradeRecord,
    EquityPoint,
    OptionsBacktestRequest,
    SignalLogEntry,
)
from app.backtest.session import BacktestOrchestrator

router = APIRouter(prefix="/api/v1/backtest", tags=["backtest"])


def get_backtest_orchestrator(request: Request) -> BacktestOrchestrator:
    """The shared `BacktestOrchestrator` constructed once at application
    startup (see `app.main`'s lifespan) — it holds only a read-only
    broker reference; every run still gets its own fresh, isolated
    `PaperTradingEngine`.
    """

    return request.app.state.backtest_orchestrator  # type: ignore[no-any-return]


@router.post("/run", status_code=status.HTTP_201_CREATED)
async def run_backtest(
    body: BacktestRequest,
    orchestrator: Annotated[BacktestOrchestrator, Depends(get_backtest_orchestrator)],
) -> BacktestResult:
    """Replay one equity/index symbol's historical session.

    Raises (mapped to HTTP responses by `app.core.exception_handlers`):
        FutureDateError: 400, `historical_date` hasn't fully closed yet.
        NoHistoricalDataError: 404, the broker had no candles for this
            symbol/date/interval (e.g. a market holiday).
        InvalidHistoricalDataError / BrokerAPIError / BrokerConnectionError:
            502/503, the broker call itself failed.
    """

    return await orchestrator.run_single(body)


@router.post("/run-options", status_code=status.HTTP_201_CREATED)
async def run_options_backtest(
    body: OptionsBacktestRequest,
    orchestrator: Annotated[BacktestOrchestrator, Depends(get_backtest_orchestrator)],
) -> BacktestResult:
    """Replay one options underlying's historical session — see
    `app.backtest.options_replay`'s module docstring for the documented
    contract-resolution/historical-data limitations this endpoint may
    report in the response's `warnings`.

    Raises:
        OptionsInfrastructureUnavailableError: 503, the configured broker
            isn't Angel One (no option chain service available).
        (plus every exception `run_backtest` above can raise.)
    """

    return await orchestrator.run_options_single(body)


@router.post("/run-batch", status_code=status.HTTP_201_CREATED)
async def run_backtest_batch(
    body: list[BacktestRequest],
    orchestrator: Annotated[BacktestOrchestrator, Depends(get_backtest_orchestrator)],
) -> AggregateBacktestResult:
    """Replay several historical sessions, each with its own fresh
    capital/engine, and return their combined statistics.
    """

    return await orchestrator.run_batch(body)


@router.get("/runs")
async def list_backtest_runs(
    orchestrator: Annotated[BacktestOrchestrator, Depends(get_backtest_orchestrator)],
) -> list[str]:
    return orchestrator.list_run_ids()


@router.get("/runs/{run_id}")
async def get_backtest_run(
    run_id: str,
    orchestrator: Annotated[BacktestOrchestrator, Depends(get_backtest_orchestrator)],
) -> BacktestResult:
    result = orchestrator.load_run(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no backtest run found for run_id={run_id!r}")
    return result


@router.get("/runs/{run_id}/trades")
async def get_backtest_trades(
    run_id: str,
    orchestrator: Annotated[BacktestOrchestrator, Depends(get_backtest_orchestrator)],
) -> list[BacktestTradeRecord]:
    result = orchestrator.load_run(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no backtest run found for run_id={run_id!r}")
    return result.trades


@router.get("/runs/{run_id}/equity-curve")
async def get_backtest_equity_curve(
    run_id: str,
    orchestrator: Annotated[BacktestOrchestrator, Depends(get_backtest_orchestrator)],
) -> list[EquityPoint]:
    result = orchestrator.load_run(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no backtest run found for run_id={run_id!r}")
    return result.equity_curve


@router.get("/runs/{run_id}/signals")
async def get_backtest_signals(
    run_id: str,
    orchestrator: Annotated[BacktestOrchestrator, Depends(get_backtest_orchestrator)],
) -> list[SignalLogEntry]:
    result = orchestrator.load_run(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no backtest run found for run_id={run_id!r}")
    return result.signal_log


@router.get("/aggregate-runs/{run_id}")
async def get_backtest_aggregate_run(
    run_id: str,
    orchestrator: Annotated[BacktestOrchestrator, Depends(get_backtest_orchestrator)],
) -> AggregateBacktestResult:
    result = orchestrator.load_aggregate_run(run_id)
    if result is None:
        raise HTTPException(
            status_code=404, detail=f"no aggregate backtest run found for run_id={run_id!r}"
        )
    return result
