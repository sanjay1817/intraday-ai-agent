"""Strategy Runtime: builds a `StrategyContext` for one symbol from
already-fetched candles, runs every registered strategy against it, and
merges their individual `StrategySignal`s into one `ConfluenceResult`.

Three strategies are wired in explicitly (not via a `pkgutil` registry
like `app.indicators`'s — see `app.strategy.strategies`'s docstring for
why) and run unconditionally; there is no filter/gating layer here
(`market_filter.py`/`session_filter.py` from the original Phase 6 plan)
because this milestone's scope is producing a recommendation for a
human to evaluate, not autonomously deciding whether a trade is
permitted — that judgment stays with the trader reading the output.

`account` is always a neutral, non-breaching `AccountRiskState`: no
account/portfolio tracking exists anywhere in this codebase yet (there
is no Paper Trading or Portfolio Analytics engine behind this milestone),
and none of the three strategies read `context.account` — the field
exists only because `StrategyContext` requires it.

`RankedSignal` (from the original Phase 6 plan) is deliberately not
produced here: ranking exists to choose among multiple *competing*
signals (e.g. across many scanned symbols), and this engine analyzes
exactly one symbol per call — there is nothing to rank against.
"""

from collections.abc import Sequence
from typing import Any

from app.domain.enums.trading import Exchange, HistoricalInterval
from app.indicators.engine import IndicatorEngine, IndicatorRequest
from app.indicators.schemas import IndicatorResult
from app.market.dto import MarketCandle, MarketSessionState
from app.market.indicator_runtime import compute_indicators
from app.strategy.base_strategy import BaseStrategy
from app.strategy.dto import AccountRiskState, StrategyContext, TimeframeSnapshot
from app.strategy.models import ConfluenceResult, SignalDirection, StrategySignal
from app.strategy.strategies.ema_trend import EMATrendStrategy
from app.strategy.strategies.rsi_macd_reversal import RSIMACDReversalStrategy
from app.strategy.strategies.vwap_volume_breakout import VWAPVolumeBreakoutStrategy

#: No account/portfolio tracking exists in this milestone; a very large
#: `max_daily_loss` means the (unused) daily-loss check this field was
#: designed for can never appear to trip.
_NEUTRAL_ACCOUNT_STATE = AccountRiskState(todays_pnl=0.0, max_daily_loss=1_000_000_000.0)


def _default_strategies() -> list[BaseStrategy]:
    return [EMATrendStrategy(), RSIMACDReversalStrategy(), VWAPVolumeBreakoutStrategy()]


class StrategyEngine:
    """Runs every registered strategy against one symbol's market data
    and merges the results into a `ConfluenceResult`.
    """

    def __init__(self, strategies: Sequence[BaseStrategy] | None = None) -> None:
        self._strategies = list(strategies) if strategies is not None else _default_strategies()

    @property
    def required_indicator_requests(self) -> list[IndicatorRequest]:
        """Every registered strategy's required indicators, deduplicated
        by result key (two strategies could coincidentally request the
        exact same indicator/params/alias) — compute this once per
        `analyze_symbol` call, not once per strategy.
        """

        by_result_key: dict[str, IndicatorRequest] = {}
        for strategy in self._strategies:
            for request in strategy.required_indicators:
                by_result_key[request.result_key] = request
        return list(by_result_key.values())

    def analyze_symbol(
        self,
        symbol: str,
        exchange: Exchange,
        interval: HistoricalInterval,
        candles: Sequence[MarketCandle],
        session: MarketSessionState,
        *,
        indicator_engine: IndicatorEngine | None = None,
    ) -> ConfluenceResult:
        """Analyze `symbol` on `interval` and return the merged
        confluence view across every registered strategy.

        Args:
            symbol: The identifier `candles` were fetched for (whatever
                form the broker that produced them expects).
            exchange: Which exchange/segment `symbol` trades on.
            interval: The candle interval `candles` are at, and the
                timeframe every strategy analyzes.
            candles: OHLCV history, in ascending timestamp order (as
                `app.market.ingestion.fetch_recent_candles` returns it).
            session: The current market session state (informational —
                strategies don't gate on it; see this module's docstring).
            indicator_engine: The `IndicatorEngine` to compute through;
                defaults to a fresh instance (safe given the engine's
                cache isn't thread-safe — see the architecture audit).

        Raises:
            InvalidOHLCVDataError: `candles` is empty.
        """

        engine = indicator_engine if indicator_engine is not None else IndicatorEngine()
        indicators = compute_indicators(engine, candles, self.required_indicator_requests)

        context = _build_context(symbol, exchange, interval, candles, indicators, session)
        signals = [strategy.analyze(context) for strategy in self._strategies]

        return _merge_into_confluence(symbol, interval, signals)


def _build_context(
    symbol: str,
    exchange: Exchange,
    interval: HistoricalInterval,
    candles: Sequence[MarketCandle],
    indicators: dict[str, IndicatorResult[Any]],
    session: MarketSessionState,
) -> StrategyContext:
    snapshot = TimeframeSnapshot(interval=interval, candles=list(candles), indicators=indicators)
    return StrategyContext(
        symbol=symbol,
        exchange=exchange,
        session=session,
        primary_timeframe=interval,
        timeframes={interval: snapshot},
        account=_NEUTRAL_ACCOUNT_STATE,
    )


def _merge_into_confluence(
    symbol: str, timeframe: HistoricalInterval, signals: Sequence[StrategySignal]
) -> ConfluenceResult:
    """Majority-vote merge: whichever direction (BULLISH/BEARISH) has
    strictly more agreeing strategies than the other wins; a tie
    (including 0-0, when every strategy returned NONE) merges to NONE.
    """

    bullish = [signal for signal in signals if signal.direction is SignalDirection.BULLISH]
    bearish = [signal for signal in signals if signal.direction is SignalDirection.BEARISH]

    if len(bullish) > len(bearish) and bullish:
        direction = SignalDirection.BULLISH
        agreeing, conflicting = bullish, bearish
    elif len(bearish) > len(bullish) and bearish:
        direction = SignalDirection.BEARISH
        agreeing, conflicting = bearish, bullish
    else:
        direction = SignalDirection.NONE
        agreeing, conflicting = [], bullish + bearish

    combined_confidence = (
        sum(signal.confidence for signal in agreeing) / len(agreeing) if agreeing else 0.0
    )

    return ConfluenceResult(
        symbol=symbol,
        timeframe=timeframe,
        direction=direction,
        agreeing_strategies=[signal.strategy_name for signal in agreeing],
        conflicting_strategies=[signal.strategy_name for signal in conflicting],
        confirmation_count=len(agreeing),
        combined_confidence=combined_confidence,
        signals=list(signals),
    )
