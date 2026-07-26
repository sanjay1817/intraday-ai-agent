"""Unit tests for `app.market.ingestion`."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import create_autospec

import pytest

from app.brokers.base import BrokerInterface
from app.domain.entities.broker import HistoricalBar
from app.domain.enums.trading import BrokerName, Exchange, HistoricalInterval
from app.domain.exceptions.market import InvalidHistoricalDataError, NoHistoricalDataError
from app.market.dto import MarketSessionState
from app.market.ingestion import current_session_state, fetch_recent_candles


def _broker() -> Any:
    """A `BrokerInterface`-shaped autospec mock.

    Typed `Any` rather than `BrokerInterface`: callers need both the
    interface's methods (to pass this into `fetch_recent_candles`, which
    expects a `BrokerInterface`) and mock-only attributes (`.return_value`,
    `.call_args`) that no real `BrokerInterface` implementation has.
    """

    return create_autospec(BrokerInterface, instance=True)


def _bar(
    timestamp: datetime,
    *,
    open_: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.5,
    volume: int = 1000,
) -> HistoricalBar:
    return HistoricalBar(
        timestamp=timestamp, open=open_, high=high, low=low, close=close, volume=volume
    )


async def test_fetch_recent_candles_converts_bars_to_market_candles() -> None:
    broker = _broker()
    bars = [
        _bar(datetime(2024, 1, 1, 9, 15, tzinfo=UTC), close=100.5),
        _bar(datetime(2024, 1, 1, 9, 20, tzinfo=UTC), close=101.0),
    ]
    broker.historical_data.return_value = bars

    candles = await fetch_recent_candles(
        broker, BrokerName.ZERODHA, Exchange.NSE, "256265", HistoricalInterval.FIVE_MINUTE
    )

    assert len(candles) == 2
    assert candles[0].symbol == "256265"
    assert candles[0].exchange is Exchange.NSE
    assert candles[0].interval is HistoricalInterval.FIVE_MINUTE
    assert candles[0].close == 100.5
    assert candles[1].close == 101.0


async def test_fetch_recent_candles_sorts_out_of_order_bars() -> None:
    broker = _broker()
    later = _bar(datetime(2024, 1, 1, 9, 20, tzinfo=UTC))
    earlier = _bar(datetime(2024, 1, 1, 9, 15, tzinfo=UTC))
    broker.historical_data.return_value = [later, earlier]

    candles = await fetch_recent_candles(
        broker, BrokerName.ZERODHA, Exchange.NSE, "256265", HistoricalInterval.FIVE_MINUTE
    )

    assert [candle.timestamp for candle in candles] == [earlier.timestamp, later.timestamp]


async def test_fetch_recent_candles_raises_on_empty_broker_response() -> None:
    broker = _broker()
    broker.historical_data.return_value = []

    with pytest.raises(NoHistoricalDataError) as exc_info:
        await fetch_recent_candles(
            broker, BrokerName.UPSTOX, Exchange.NSE, "NSE_EQ|X", HistoricalInterval.FIVE_MINUTE
        )

    error = exc_info.value
    assert error.broker is BrokerName.UPSTOX
    assert error.exchange is Exchange.NSE
    assert error.tradingsymbol == "NSE_EQ|X"
    assert error.interval is HistoricalInterval.FIVE_MINUTE


async def test_fetch_recent_candles_raises_on_invalid_bar() -> None:
    broker = _broker()
    broker.historical_data.return_value = [
        _bar(datetime(2024, 1, 1, 9, 15, tzinfo=UTC), high=90.0, low=95.0)
    ]

    with pytest.raises(InvalidHistoricalDataError) as exc_info:
        await fetch_recent_candles(
            broker, BrokerName.ANGEL_ONE, Exchange.NSE, "3045", HistoricalInterval.FIVE_MINUTE
        )

    error = exc_info.value
    assert error.broker is BrokerName.ANGEL_ONE
    assert error.tradingsymbol == "3045"
    assert "low" in error.reason.lower() or "high" in error.reason.lower()


async def test_fetch_recent_candles_uses_generous_daily_lookback_for_daily_interval() -> None:
    broker = _broker()
    broker.historical_data.return_value = [_bar(datetime(2024, 6, 1, tzinfo=UTC))]
    now = datetime(2024, 6, 15, tzinfo=UTC)

    await fetch_recent_candles(
        broker, BrokerName.ZERODHA, Exchange.NSE, "256265", HistoricalInterval.ONE_DAY, now=now
    )

    call_args = broker.historical_data.call_args
    from_date = call_args.args[3]
    assert (now - from_date).days == 365


async def test_fetch_recent_candles_uses_shorter_lookback_for_intraday_interval() -> None:
    broker = _broker()
    broker.historical_data.return_value = [_bar(datetime(2024, 6, 14, tzinfo=UTC))]
    now = datetime(2024, 6, 15, tzinfo=UTC)

    await fetch_recent_candles(
        broker, BrokerName.ZERODHA, Exchange.NSE, "256265", HistoricalInterval.FIVE_MINUTE, now=now
    )

    call_args = broker.historical_data.call_args
    from_date = call_args.args[3]
    assert (now - from_date).days == 10


async def test_fetch_recent_candles_respects_explicit_lookback_override() -> None:
    broker = _broker()
    broker.historical_data.return_value = [_bar(datetime(2024, 6, 14, tzinfo=UTC))]
    now = datetime(2024, 6, 15, tzinfo=UTC)

    await fetch_recent_candles(
        broker,
        BrokerName.ZERODHA,
        Exchange.NSE,
        "256265",
        HistoricalInterval.FIVE_MINUTE,
        lookback_days=3,
        now=now,
    )

    call_args = broker.historical_data.call_args
    from_date = call_args.args[3]
    assert (now - from_date).days == 3


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2024, 6, 3, 0, 30, tzinfo=UTC), MarketSessionState.PRE_OPEN),  # Mon, 06:00 IST
        (datetime(2024, 6, 3, 6, 30, tzinfo=UTC), MarketSessionState.OPEN),  # Mon, 12:00 IST
        (datetime(2024, 6, 3, 12, 30, tzinfo=UTC), MarketSessionState.CLOSED),  # Mon, 18:00 IST
        (datetime(2024, 6, 8, 6, 30, tzinfo=UTC), MarketSessionState.CLOSED),  # Sat, 12:00 IST
    ],
)
def test_current_session_state(moment: datetime, expected: MarketSessionState) -> None:
    assert current_session_state(moment) is expected
