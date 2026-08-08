"""Shared fixtures/helpers for `app.backtest` unit tests."""

from datetime import UTC, datetime, timedelta

from app.domain.enums.trading import Exchange, HistoricalInterval
from app.market.dto import MarketCandle

SYMBOL = "TESTSTOCK-EQ"
EXCHANGE = Exchange.NSE
INTERVAL = HistoricalInterval.ONE_MINUTE


def make_trending_candles(
    n: int,
    *,
    start_price: float = 100.0,
    trend_per_bar: float = 0.004,
    start: datetime | None = None,
) -> list[MarketCandle]:
    """A strong, steady uptrend over `n` one-minute bars — enough history
    for EMA(21)/ADX(14)/SuperTrend(10) to warm up and for
    `EMATrendStrategy` to find a confirmed trend, without claiming to
    model real market microstructure (mirrors
    `tests/unit/strategy/strategies/conftest.py::make_candles`'s own
    mechanical open/high/low derivation).
    """

    start_time = start or datetime(2026, 8, 5, 9, 15, tzinfo=UTC)
    candles: list[MarketCandle] = []
    close = start_price
    for i in range(n):
        open_ = close
        close = open_ * (1 + trend_per_bar)
        high = close * 1.001
        low = open_ * 0.999
        candles.append(
            MarketCandle(
                symbol=SYMBOL,
                exchange=EXCHANGE,
                interval=INTERVAL,
                timestamp=start_time + timedelta(minutes=i),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=1_000 + i,
            )
        )
    return candles


def make_flat_candles(
    n: int, *, price: float = 100.0, start: datetime | None = None
) -> list[MarketCandle]:
    """A perfectly flat series — no strategy should ever fire a
    directional signal against this, useful as a deterministic
    HOLD-only baseline.
    """

    start_time = start or datetime(2026, 8, 5, 9, 15, tzinfo=UTC)
    return [
        MarketCandle(
            symbol=SYMBOL,
            exchange=EXCHANGE,
            interval=INTERVAL,
            timestamp=start_time + timedelta(minutes=i),
            open=price,
            high=price * 1.0001,
            low=price * 0.9999,
            close=price,
            volume=1_000,
        )
        for i in range(n)
    ]
