"""Unit tests for `app.backtest.dataset_manager.BrokerHistoricalDataProvider`."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.backtest.dataset_manager import BrokerHistoricalDataProvider
from app.domain.entities.broker import HistoricalBar
from app.domain.enums.trading import BrokerName, Exchange, HistoricalInterval
from app.domain.exceptions.market import NoHistoricalDataError

_FROM = datetime(2026, 8, 5, 9, 15, tzinfo=UTC)
_TO = datetime(2026, 8, 5, 15, 30, tzinfo=UTC)


class _FakeBroker:
    """Only implements `historical_data` — anything else is unused by
    `BrokerHistoricalDataProvider`, matching the codebase's established
    fake-broker convention (see `tests/unit/auto/test_orchestrator.py`).
    """

    def __init__(self, bars: list[HistoricalBar]) -> None:
        self.bars = bars
        self.call_count = 0

    async def historical_data(
        self,
        exchange: Exchange,
        tradingsymbol: str,
        interval: HistoricalInterval,
        from_date: datetime,
        to_date: datetime,
    ) -> list[HistoricalBar]:
        self.call_count += 1
        return self.bars


def _bar(minute: int, price: float) -> HistoricalBar:
    return HistoricalBar(
        timestamp=_FROM + timedelta(minutes=minute),
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=1_000,
    )


@pytest.mark.asyncio
async def test_get_candles_converts_bars_to_market_candles() -> None:
    broker = _FakeBroker([_bar(0, 100.0), _bar(1, 101.0)])
    provider = BrokerHistoricalDataProvider(broker, BrokerName.ANGEL_ONE)  # type: ignore[arg-type]

    candles = await provider.get_candles(
        "RELIANCE-EQ", Exchange.NSE, HistoricalInterval.ONE_MINUTE, _FROM, _TO
    )

    assert len(candles) == 2
    assert candles[0].close == 100.0
    assert candles[1].close == 101.0
    assert candles == sorted(candles, key=lambda candle: candle.timestamp)


@pytest.mark.asyncio
async def test_empty_broker_response_raises_no_historical_data_error_not_fabricated_prices() -> None:
    broker = _FakeBroker([])
    provider = BrokerHistoricalDataProvider(broker, BrokerName.ANGEL_ONE)  # type: ignore[arg-type]

    with pytest.raises(NoHistoricalDataError):
        await provider.get_candles(
            "RELIANCE-EQ", Exchange.NSE, HistoricalInterval.ONE_MINUTE, _FROM, _TO
        )


@pytest.mark.asyncio
async def test_disk_cache_avoids_a_second_broker_call(tmp_path: Path) -> None:
    broker = _FakeBroker([_bar(0, 100.0), _bar(1, 101.0)])
    provider = BrokerHistoricalDataProvider(
        broker, BrokerName.ANGEL_ONE, cache_dir=tmp_path  # type: ignore[arg-type]
    )

    first = await provider.get_candles(
        "RELIANCE-EQ", Exchange.NSE, HistoricalInterval.ONE_MINUTE, _FROM, _TO
    )
    second = await provider.get_candles(
        "RELIANCE-EQ", Exchange.NSE, HistoricalInterval.ONE_MINUTE, _FROM, _TO
    )

    assert broker.call_count == 1
    assert first == second


@pytest.mark.asyncio
async def test_cache_is_keyed_by_symbol_interval_and_date_range(tmp_path: Path) -> None:
    broker = _FakeBroker([_bar(0, 100.0)])
    provider = BrokerHistoricalDataProvider(
        broker, BrokerName.ANGEL_ONE, cache_dir=tmp_path  # type: ignore[arg-type]
    )

    await provider.get_candles("RELIANCE-EQ", Exchange.NSE, HistoricalInterval.ONE_MINUTE, _FROM, _TO)
    await provider.get_candles("SBIN-EQ", Exchange.NSE, HistoricalInterval.ONE_MINUTE, _FROM, _TO)

    assert broker.call_count == 2
