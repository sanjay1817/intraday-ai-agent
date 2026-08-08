"""Backtest Orchestrator: the single entry point that ties the dataset
manager, replay engine, metrics, and result storage together for one (or
several) historical trading sessions.

This is the one class `app.api.v1.routers.backtest` calls into. It holds
only a read-only `BrokerInterface` (via `BrokerHistoricalDataProvider`,
`historical_data()` only) and constructs a brand-new, isolated
`PaperTradingEngine` per run inside `app.backtest.replay_engine`/
`app.backtest.options_replay` — never `app.state.paper_engine`, never a
live order-placement call. There is no code path from this class to a
real broker order endpoint.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog

from app.backtest.dataset_manager import BrokerHistoricalDataProvider
from app.backtest.dto import (
    AggregateBacktestResult,
    AggregateBacktestSummary,
    BacktestRequest,
    BacktestResult,
    OptionsBacktestRequest,
)
from app.backtest.metrics import summarize
from app.backtest.options_replay import run_options_replay
from app.backtest.replay_engine import run_replay
from app.backtest.storage import BacktestResultStore
from app.brokers.base import BrokerInterface
from app.domain.enums.trading import BrokerName
from app.options.models import ExpiryMode, StrikeMode
from app.options.option_chain_service import OptionChainService
from app.options.underlying_resolver import UnderlyingResolver

logger = structlog.get_logger(__name__)

_INDIA_TZ = ZoneInfo("Asia/Kolkata")


def _session_window(request: BacktestRequest) -> tuple[datetime, datetime]:
    from_dt = datetime.combine(request.historical_date, request.start_time, tzinfo=_INDIA_TZ)
    to_dt = datetime.combine(request.historical_date, request.end_time, tzinfo=_INDIA_TZ)
    return from_dt, to_dt


class BacktestOrchestrator:
    def __init__(
        self,
        broker: BrokerInterface,
        broker_name: BrokerName,
        *,
        data_dir: Path,
        option_chain_service: OptionChainService | None = None,
        underlying_resolver: UnderlyingResolver | None = None,
    ) -> None:
        self._dataset_provider = BrokerHistoricalDataProvider(
            broker, broker_name, cache_dir=data_dir / "candles"
        )
        self._store = BacktestResultStore(data_dir / "results")
        self._option_chain_service = option_chain_service
        self._underlying_resolver = underlying_resolver

    async def run_single(
        self, request: BacktestRequest, *, today: date | None = None
    ) -> BacktestResult:
        """Replay one equity/index symbol's session and persist the result."""

        request.check_not_future_dated(today=today)
        from_dt, to_dt = _session_window(request)

        candles = await self._dataset_provider.get_candles(
            request.symbol, request.exchange, request.interval, from_dt, to_dt
        )

        trades, signal_log, equity_curve, warnings = await run_replay(
            candles,
            symbol=request.symbol,
            exchange=request.exchange,
            interval=request.interval,
            initial_capital=request.initial_capital,
            confidence_threshold=request.confidence_threshold,
            capital_fraction_per_trade=request.capital_fraction_per_trade,
            cost_model=request.cost_model,
        )

        summary = summarize(trades, equity_curve, initial_capital=request.initial_capital)
        result = BacktestResult(
            request=request,
            summary=summary,
            trades=trades,
            equity_curve=equity_curve,
            signal_log=signal_log,
            warnings=warnings,
            generated_at=datetime.now(UTC),
        )
        self._store.save(result)
        logger.info(
            "backtest_run_completed",
            run_id=result.run_id,
            symbol=request.symbol,
            historical_date=request.historical_date.isoformat(),
            total_trades=summary.total_trades,
            total_pnl=summary.total_pnl,
        )
        return result

    async def run_options_single(
        self, request: OptionsBacktestRequest, *, today: date | None = None
    ) -> BacktestResult:
        """Replay one options underlying's session (see
        `app.backtest.options_replay`'s module docstring for its known
        contract-resolution limitation) and persist the result.
        """

        request.check_not_future_dated(today=today)
        if self._option_chain_service is None or self._underlying_resolver is None:
            from app.options.exceptions import OptionsInfrastructureUnavailableError

            raise OptionsInfrastructureUnavailableError(
                "options backtesting requires an Angel One-backed option chain service, "
                "which is not configured for the currently selected broker"
            )

        from_dt, to_dt = _session_window(request)
        exchange, tradingsymbol = await self._underlying_resolver.resolve_historical_instrument(
            request.symbol
        )
        underlying_candles = await self._dataset_provider.get_candles(
            tradingsymbol, exchange, request.interval, from_dt, to_dt
        )

        trades, signal_log, equity_curve, warnings = await run_options_replay(
            underlying_candles,
            underlying=request.symbol,
            underlying_exchange=exchange,
            interval=request.interval,
            option_chain_service=self._option_chain_service,
            strike_mode=StrikeMode(request.strike_mode),
            expiry_mode=ExpiryMode(request.expiry_mode),
            option_dataset_provider=self._dataset_provider,
            initial_capital=request.initial_capital,
            confidence_threshold=request.confidence_threshold,
            capital_fraction_per_trade=request.capital_fraction_per_trade,
            cost_model=request.cost_model,
        )

        summary = summarize(trades, equity_curve, initial_capital=request.initial_capital)
        result = BacktestResult(
            request=request,
            summary=summary,
            trades=trades,
            equity_curve=equity_curve,
            signal_log=signal_log,
            warnings=warnings,
            generated_at=datetime.now(UTC),
        )
        self._store.save(result)
        return result

    async def run_batch(
        self, requests: list[BacktestRequest], *, today: date | None = None
    ) -> AggregateBacktestResult:
        """Run each request as its own fully independent session (its
        own fresh capital, its own `PaperTradingEngine`) and combine
        their statistics — Requirement 9's multi-date support.
        """

        session_results = [await self.run_single(request, today=today) for request in requests]

        total_trades = sum(result.summary.total_trades for result in session_results)
        winning_trades = sum(result.summary.winning_trades for result in session_results)
        losing_trades = sum(result.summary.losing_trades for result in session_results)
        total_pnl = sum(result.summary.total_pnl for result in session_results)
        win_rate = (winning_trades / total_trades * 100.0) if total_trades > 0 else 0.0
        max_drawdown = max(
            (result.summary.max_drawdown for result in session_results), default=0.0
        )

        aggregate = AggregateBacktestSummary(
            total_sessions=len(session_results),
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            total_pnl=total_pnl,
            win_rate=win_rate,
            max_drawdown=max_drawdown,
        )
        result = AggregateBacktestResult(
            session_results=session_results, aggregate=aggregate, generated_at=datetime.now(UTC)
        )
        self._store.save_aggregate(result)
        return result

    def list_run_ids(self) -> list[str]:
        return self._store.list_run_ids()

    def load_run(self, run_id: str) -> BacktestResult | None:
        return self._store.load(run_id)

    def load_aggregate_run(self, run_id: str) -> AggregateBacktestResult | None:
        return self._store.load_aggregate(run_id)
