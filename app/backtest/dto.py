"""Backtesting & Research Framework input DTOs.

`BacktestConfig` describes one backtest run. Its shape covers the full
pipeline this framework is designed for (replay → strategies → AI →
risk → paper execution), even though only the replay/data side is built
so far — `strategy_names`/`ai_enabled`/`benchmark_symbol` are accepted
as plain configuration today so this model won't need to change shape
once `strategy_runner.py`/`ai_runner.py`/`benchmark.py` exist; nothing
currently reads them.

Reuses `Exchange`/`HistoricalInterval` from `app.domain.enums.trading`
rather than re-declaring them.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums.trading import Exchange, HistoricalInterval


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
