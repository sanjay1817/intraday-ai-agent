"""Strategy Engine input DTOs: `StrategyContext`, the sole argument every
`BaseStrategy.analyze()` receives.

Deliberately narrower than `app.ai.decision_models.MarketContext`: this
engine only ever reasons about market/technical state — per its core
invariant it never talks to a broker or a database — so its context
carries multi-timeframe candles/indicators plus the minimal account-risk
figures the filter layer needs (e.g. today's PnL vs. a daily-loss limit),
supplied by the caller, never fetched by this engine itself.

Reuses `MarketCandle`/`MarketSessionState` from `app.market.dto` (the
Market Data Engine is this engine's actual upstream) and `IndicatorResult`
from `app.indicators.schemas` — the same instruments/indicators the rest
of the application already computes, not a parallel representation.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums.trading import Exchange, HistoricalInterval
from app.indicators.schemas import IndicatorResult
from app.market.dto import MarketCandle, MarketSessionState


class SymbolStatus(StrEnum):
    """Whether a symbol is currently tradable at all, independent of
    market session or setup quality — `market_filter.py`'s "symbol
    halted" check reads this.
    """

    ACTIVE = "ACTIVE"
    HALTED = "HALTED"


class TimeframeSnapshot(BaseModel):
    """Candles plus already-computed indicators for one symbol, at one
    timeframe.

    Indicators are supplied, not computed here: the Strategy Engine
    consumes `app.indicators.IndicatorEngine`'s output, it doesn't
    duplicate that computation.
    """

    model_config = ConfigDict(frozen=True)

    interval: HistoricalInterval
    candles: list[MarketCandle] = Field(min_length=1)
    indicators: dict[str, IndicatorResult[Any]] = Field(default_factory=dict)

    @property
    def latest_candle(self) -> MarketCandle:
        """The most recent candle in this snapshot."""

        return self.candles[-1]


class AccountRiskState(BaseModel):
    """The minimal account-level figures the filter layer needs (e.g.
    `market_filter.py`'s daily-loss-exceeded check).

    Nothing else about the account (positions, capital, broker identity)
    belongs here — the Strategy Engine reasons about whether *today's*
    realized PnL has already breached the configured limit, not about
    account state generally.
    """

    model_config = ConfigDict(frozen=True)

    todays_pnl: float
    max_daily_loss: float = Field(gt=0)


class StrategyContext(BaseModel):
    """Everything a strategy's `analyze()` is allowed to see.

    `timeframes` covers whichever of 1m/3m/5m/15m/30m/1h/daily the caller
    fetched for this symbol; multi-timeframe strategies and
    `trend_filter.py`'s higher-timeframe-alignment check read several
    entries, single-timeframe strategies read only `primary_timeframe`.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    exchange: Exchange
    status: SymbolStatus = SymbolStatus.ACTIVE
    session: MarketSessionState
    spread_percent: float | None = Field(default=None, ge=0)
    primary_timeframe: HistoricalInterval
    timeframes: dict[HistoricalInterval, TimeframeSnapshot] = Field(min_length=1)
    account: AccountRiskState

    @model_validator(mode="after")
    def _check_primary_timeframe_present(self) -> "StrategyContext":
        """A context claiming `primary_timeframe=FIVE_MINUTE` but not
        actually carrying that timeframe's data is internally
        inconsistent — every strategy assumes `primary_snapshot` exists.
        """

        if self.primary_timeframe not in self.timeframes:
            raise ValueError(
                f"primary_timeframe {self.primary_timeframe!r} has no entry in `timeframes`"
            )
        return self

    @property
    def primary_snapshot(self) -> TimeframeSnapshot:
        """The `TimeframeSnapshot` for `primary_timeframe` — the
        timeframe a signal is ultimately generated for.
        """

        return self.timeframes[self.primary_timeframe]

    @property
    def latest_candle(self) -> MarketCandle:
        """The most recent candle on the primary timeframe."""

        return self.primary_snapshot.latest_candle

    def snapshot(self, interval: HistoricalInterval) -> TimeframeSnapshot | None:
        """The `TimeframeSnapshot` for `interval`, or `None` if the
        caller didn't fetch that timeframe for this symbol.
        """

        return self.timeframes.get(interval)
