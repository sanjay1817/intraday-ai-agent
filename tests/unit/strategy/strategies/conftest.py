"""Shared fixtures/helpers for concrete-strategy unit tests."""

from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.enums.trading import Exchange, HistoricalInterval
from app.indicators.schemas import IndicatorResult
from app.market.dto import MarketCandle, MarketSessionState
from app.strategy.dto import AccountRiskState, StrategyContext, TimeframeSnapshot

SYMBOL = "TEST"
INTERVAL = HistoricalInterval.FIVE_MINUTE


def make_candles(closes: list[float], *, volumes: list[int] | None = None) -> list[MarketCandle]:
    """Build a synthetic candle series from closing prices, deriving
    open/high/low mechanically (open = previous close; high/low a small
    fixed percentage outside open/close) — enough to exercise real
    indicator computation, without claiming to model real market
    microstructure.
    """

    resolved_volumes = volumes or [1000] * len(closes)
    start = datetime(2024, 1, 1, 9, 15, tzinfo=UTC)
    candles = []
    for i, close in enumerate(closes):
        open_ = closes[i - 1] if i > 0 else close
        high = max(open_, close) * 1.002
        low = min(open_, close) * 0.998
        candles.append(
            MarketCandle(
                symbol=SYMBOL,
                exchange=Exchange.NSE,
                interval=INTERVAL,
                timestamp=start + timedelta(minutes=5 * i),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=resolved_volumes[i],
            )
        )
    return candles


def make_context(
    candles: list[MarketCandle], indicators: dict[str, IndicatorResult[Any]]
) -> StrategyContext:
    """Build a minimal, valid `StrategyContext` around `candles`/`indicators`.

    `account` is a neutral, always-non-breaching value: no account/PnL
    tracking exists in this milestone (see `app.strategy.engine`'s own
    docstring), and no strategy reads `context.account` — it exists only
    because `StrategyContext` requires the field.
    """

    snapshot = TimeframeSnapshot(interval=INTERVAL, candles=candles, indicators=indicators)
    return StrategyContext(
        symbol=SYMBOL,
        exchange=Exchange.NSE,
        session=MarketSessionState.OPEN,
        primary_timeframe=INTERVAL,
        timeframes={INTERVAL: snapshot},
        account=AccountRiskState(todays_pnl=0.0, max_daily_loss=1_000_000.0),
    )


def single_result(point: Any, *, name: str = "X") -> IndicatorResult[Any]:
    """An `IndicatorResult` wrapping a single hand-crafted point — for
    edge-case tests that need precise control over one indicator's
    value rather than real pandas-ta computation.
    """

    return IndicatorResult(name=name, params={}, values=[point])
