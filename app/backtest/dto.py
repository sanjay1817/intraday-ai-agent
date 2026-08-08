"""Backtesting & Research Framework DTOs.

`BacktestConfig` describes one backtest run. Its shape covers the full
pipeline this framework is designed for (replay → strategies → AI →
risk → paper execution), even though only the replay/data side is built
so far — `strategy_names`/`ai_enabled`/`benchmark_symbol` are accepted
as plain configuration today so this model won't need to change shape
once `strategy_runner.py`/`ai_runner.py`/`benchmark.py` exist; nothing
currently reads them.

`BacktestRequest` is the caller-facing shape (`POST /api/v1/backtest/run`
and the "Historical" mode form): a plain date + start/end wall-clock time
+ symbol + capital, the same inputs a human picks when replaying one past
trading session. `app.backtest.session.BacktestOrchestrator` turns it
into a `BacktestConfig` (combining `historical_date` with `start_time`/
`end_time` into `from_date`/`to_date`) before running the replay.

`BacktestTradeRecord`/`SignalLogEntry`/`EquityPoint`/`BacktestSummary`/
`BacktestResult` are the reporting output — deliberately separate from
`app.paper.models.ClosedTrade`/`Portfolio` (which have no room for
charges/slippage/exit-reason/drawdown) so the shared Paper Trading Engine
models never need to change shape for this one feature.

Reuses `Exchange`/`HistoricalInterval`/`OrderSide` from
`app.domain.enums.trading` rather than re-declaring them.
"""

from datetime import UTC, date, datetime, time
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.backtest.costs import TransactionCostModel
from app.domain.enums.trading import Exchange, HistoricalInterval, OrderSide
from app.domain.exceptions.backtest import FutureDateError


class ReplayMode(StrEnum):
    """Whether `app.backtest.replay_engine` steps through raw ticks or
    pre-built candles.

    Tick replay is the higher-fidelity, slower option — every trade is
    replayed individually, exercising the same tick-level path live
    trading would. Candle replay steps bar-by-bar: cheaper, and
    sufficient for strategies that only ever read closed candles.
    """

    TICK = "TICK"
    CANDLE = "CANDLE"


class ReplaySpeed(StrEnum):
    """How fast the simulation clock advances relative to historical
    time. See `app.backtest.simulation_clock` for the multiplier each
    value maps to.
    """

    X1 = "X1"
    X5 = "X5"
    X10 = "X10"
    X50 = "X50"
    X100 = "X100"


class BacktestConfig(BaseModel):
    """One backtest run's configuration."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    exchange: Exchange
    from_date: datetime
    to_date: datetime
    replay_mode: ReplayMode = ReplayMode.CANDLE
    candle_interval: HistoricalInterval = HistoricalInterval.FIVE_MINUTE
    replay_speed: ReplaySpeed = ReplaySpeed.X1
    initial_capital: float = Field(gt=0)
    strategy_names: list[str] = Field(default_factory=list)
    ai_enabled: bool = False
    benchmark_symbol: str | None = None

    @model_validator(mode="after")
    def _check_date_range(self) -> "BacktestConfig":
        if self.from_date >= self.to_date:
            raise ValueError("from_date must be before to_date")
        return self


class DatasetDescriptor(BaseModel):
    """Metadata `app.backtest.dataset_manager` tracks for one stored
    historical dataset — used to detect an already-downloaded range
    (avoiding duplicate imports) and to verify integrity on load.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    exchange: Exchange
    interval: HistoricalInterval
    from_date: datetime
    to_date: datetime
    row_count: int = Field(ge=0)
    content_hash: str = Field(min_length=1)
    downloaded_at: datetime

    @model_validator(mode="after")
    def _check_date_range(self) -> "DatasetDescriptor":
        if self.from_date >= self.to_date:
            raise ValueError("from_date must be before to_date")
        return self


class BacktestRequest(BaseModel):
    """One caller-facing request to replay a single past trading session.

    `historical_date` must not be in the future (checked against `today`,
    which callers can override for deterministic testing) — a backtest
    can only ever replay a session that has already happened.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    exchange: Exchange
    historical_date: date
    start_time: time = time(9, 15)
    end_time: time = time(15, 30)
    interval: HistoricalInterval = HistoricalInterval.ONE_MINUTE
    initial_capital: float = Field(gt=0)

    #: Minimum `InstructorRecommendation.confidence` the replay acts on
    #: for a new entry — mirrors `Settings.auto_confidence_threshold`'s
    #: exact purpose for the live orchestrator.
    confidence_threshold: float = Field(default=60.0, ge=0, le=100)

    #: Fraction of available cash committed to a single new entry —
    #: mirrors `AutoTradingConfig.capital_fraction_per_trade`.
    capital_fraction_per_trade: float = Field(default=1.0, gt=0, le=1)

    cost_model: TransactionCostModel = Field(default_factory=TransactionCostModel)

    @model_validator(mode="after")
    def _check_time_range(self) -> "BacktestRequest":
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be before end_time")
        return self

    def check_not_future_dated(self, *, today: date | None = None) -> None:
        """Raises `FutureDateError` if `historical_date` is today or later.

        Not a `model_validator` because "today" depends on wall-clock
        time, not just the request's own fields — `app.backtest.session`
        calls this explicitly (with an injectable `today` for tests)
        rather than pydantic validating against a hidden clock read.
        """

        resolved_today = today if today is not None else datetime.now(UTC).date()
        if self.historical_date >= resolved_today:
            raise FutureDateError(
                f"historical_date ({self.historical_date}) must be strictly before "
                f"today ({resolved_today}) — a backtest can only replay a session "
                "that has already fully closed"
            )


class OptionsBacktestRequest(BacktestRequest):
    """A `BacktestRequest` for an options underlying: the strategy runs
    against `symbol` (the underlying, e.g. `"NIFTY"`) exactly as
    `app.options.recommendation.generate_option_recommendation` does, but
    the resulting BUY/SELL is translated to CE/PE and priced using the
    *option contract's own* historical data — see
    `app.backtest.options_replay`'s module docstring for why this is
    frequently unavailable and never falls back to the underlying's price.
    """

    strike_mode: str = "ATM"
    expiry_mode: str = "NEAREST_WEEKLY"


class BacktestTradeRecord(BaseModel):
    """One completed round-trip trade from a historical replay —
    `app.paper.models.ClosedTrade` plus the charges/slippage/exit-reason
    detail this feature's reporting requirements call for.
    """

    model_config = ConfigDict(frozen=True)

    trade_id: str = Field(default_factory=lambda: str(uuid4()))
    symbol: str
    exchange: Exchange
    side: OrderSide
    quantity: int = Field(gt=0)
    entry_time: datetime
    entry_signal_price: float = Field(gt=0)
    entry_fill_price: float = Field(gt=0)
    exit_time: datetime
    exit_signal_price: float = Field(gt=0)
    exit_fill_price: float = Field(gt=0)
    stop_loss: float | None = None
    target: float | None = None
    exit_reason: str
    gross_pnl: float
    charges: float = Field(ge=0)
    slippage_cost: float
    net_pnl: float
    confidence: float | None = None
    strategy_signal: str = ""


class SignalLogEntry(BaseModel):
    """One candle's strategy read — every signal generated during the
    session, shown so a caller can see *why* the bot traded (or didn't).
    """

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    action: str
    confidence: float
    #: Only indicators the current strategies actually compute (EMA9,
    #: EMA21, ADX, SuperTrend, RSI, ...) — keyed by the same alias
    #: `app.strategy.engine.StrategyEngine.required_indicator_requests`
    #: uses, `None` while an indicator is still warming up.
    indicators: dict[str, float | None] = Field(default_factory=dict)
    reasoning: str = ""


class EquityPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    equity: float
    drawdown: float = Field(ge=0)
    drawdown_percent: float = Field(ge=0)


class BacktestSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    initial_capital: float
    final_capital: float
    total_pnl: float
    total_pnl_percent: float
    total_trades: int = Field(ge=0)
    winning_trades: int = Field(ge=0)
    losing_trades: int = Field(ge=0)
    win_rate: float = Field(ge=0, le=100)
    average_profit: float
    average_loss: float
    largest_win: float
    largest_loss: float
    max_drawdown: float = Field(ge=0)
    max_drawdown_percent: float = Field(ge=0)
    profit_factor: float | None = None


#: Shown on every result so a caller (API consumer or UI) never mistakes
#: this for a live or manual-paper trade.
BACKTEST_DISCLAIMER = (
    "HISTORICAL BACKTEST — NOT LIVE TRADING, NOT MANUAL PAPER TRADING. "
    "NO REAL ORDERS PLACED. Simulated on historical candle data only; "
    "actual live/paper results may differ due to slippage, liquidity, "
    "spread, and data-quality limitations documented in `warnings`."
)


class BacktestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str = Field(default_factory=lambda: str(uuid4()))
    request: BacktestRequest
    summary: BacktestSummary
    trades: list[BacktestTradeRecord] = Field(default_factory=list)
    equity_curve: list[EquityPoint] = Field(default_factory=list)
    signal_log: list[SignalLogEntry] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    disclaimer: str = BACKTEST_DISCLAIMER
    generated_at: datetime


class AggregateBacktestSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_sessions: int = Field(ge=0)
    total_trades: int = Field(ge=0)
    winning_trades: int = Field(ge=0)
    losing_trades: int = Field(ge=0)
    total_pnl: float
    win_rate: float = Field(ge=0, le=100)
    max_drawdown: float = Field(ge=0)


class AggregateBacktestResult(BaseModel):
    """Multiple single-date `BacktestResult`s (each run fully
    independently, its own fresh capital/engine) plus the combined
    statistics across all of them.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(default_factory=lambda: str(uuid4()))
    session_results: list[BacktestResult] = Field(default_factory=list)
    aggregate: AggregateBacktestSummary
    disclaimer: str = BACKTEST_DISCLAIMER
    generated_at: datetime
