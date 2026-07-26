"""Unit tests for `app.strategy.base_strategy`."""

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from app.domain.enums.trading import Exchange, HistoricalInterval
from app.indicators.engine import IndicatorRequest
from app.market.dto import MarketCandle, MarketSessionState
from app.strategy.base_strategy import BaseStrategy
from app.strategy.dto import AccountRiskState, StrategyContext, TimeframeSnapshot
from app.strategy.models import SignalDirection, SignalStrength, StrategySignal


class _StubStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "STUB"

    @property
    def required_indicators(self) -> Sequence[IndicatorRequest]:
        return [IndicatorRequest(name="RSI", params={"length": 14})]

    def analyze(self, context: StrategyContext) -> StrategySignal:
        return StrategySignal(
            symbol=context.symbol,
            timeframe=context.primary_timeframe,
            direction=SignalDirection.NONE,
            confidence=0.0,
            strength=SignalStrength.WEAK,
            strategy_name=self.name,
            timestamp=context.latest_candle.timestamp,
        )


def _context() -> StrategyContext:
    candle = MarketCandle(
        symbol="TEST",
        exchange=Exchange.NSE,
        interval=HistoricalInterval.FIVE_MINUTE,
        timestamp=datetime(2024, 1, 1, 9, 15, tzinfo=UTC),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1000,
    )
    snapshot = TimeframeSnapshot(interval=HistoricalInterval.FIVE_MINUTE, candles=[candle])
    return StrategyContext(
        symbol="TEST",
        exchange=Exchange.NSE,
        session=MarketSessionState.OPEN,
        primary_timeframe=HistoricalInterval.FIVE_MINUTE,
        timeframes={HistoricalInterval.FIVE_MINUTE: snapshot},
        account=AccountRiskState(todays_pnl=0.0, max_daily_loss=10000.0),
    )


def test_base_strategy_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        BaseStrategy()  # type: ignore[abstract]


def test_concrete_strategy_exposes_name_and_required_indicators() -> None:
    strategy = _StubStrategy()

    assert strategy.name == "STUB"
    assert strategy.required_indicators == [IndicatorRequest(name="RSI", params={"length": 14})]


def test_concrete_strategy_analyze_returns_a_strategy_signal() -> None:
    strategy = _StubStrategy()

    signal = strategy.analyze(_context())

    assert signal.direction is SignalDirection.NONE
    assert signal.strategy_name == "STUB"
    assert signal.symbol == "TEST"
