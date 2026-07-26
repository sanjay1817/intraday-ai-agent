"""The Strategy Engine's strategy contract.

Every concrete strategy (`app.strategy.strategies.*`) implements this ABC
and nothing else — `app.strategy.engine.StrategyEngine` depends only on
`BaseStrategy`, never on a concrete strategy class, mirroring the
Adapter Pattern already established for brokers (`BrokerInterface`) and
technical indicators (`Indicator`).

`required_indicators` exists because indicators must be computed
*before* a `StrategyContext` can be built (a `TimeframeSnapshot` carries
already-computed indicators; it doesn't compute them) — `StrategyEngine`
gathers every registered strategy's requirements up front and computes
them all in one batch via `app.market.indicator_runtime.compute_indicators`,
rather than each strategy computing its own indicators independently.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.indicators.engine import IndicatorRequest
from app.strategy.dto import StrategyContext
from app.strategy.models import SignalDirection, SignalStrength, StrategySignal

#: Confidence thresholds shared by every concrete strategy's
#: `_strength_from_confidence` call — keeping one scale means a
#: "STRONG" signal means the same thing regardless of which strategy
#: produced it.
_STRONG_CONFIDENCE_THRESHOLD = 70.0
_MODERATE_CONFIDENCE_THRESHOLD = 40.0


class BaseStrategy(ABC):
    """One deterministic, rule-based trade-setup detector.

    A strategy never fetches data or computes indicators itself — both
    are supplied through `StrategyContext`, built by whatever calls
    `analyze()`. This keeps every strategy pure and independently
    testable: given the same `StrategyContext`, `analyze()` always
    returns the same `StrategySignal`.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """This strategy's unique name — becomes `StrategySignal.strategy_name`."""

    @property
    @abstractmethod
    def required_indicators(self) -> Sequence[IndicatorRequest]:
        """Which indicators this strategy needs on the primary timeframe,
        computed before `analyze()` is ever called.
        """

    @abstractmethod
    def analyze(self, context: StrategyContext) -> StrategySignal:
        """Evaluate `context` and return this strategy's signal for
        `context.primary_timeframe`.

        A `SignalDirection.NONE` signal is a normal, valid result — most
        symbols, most of the time, have no tradable setup for a given
        strategy; that isn't a failure.
        """

    def _no_signal(self, context: StrategyContext, reason: str) -> StrategySignal:
        """A `SignalDirection.NONE` result: no tradable setup, with
        `reason` recorded so a caller (or a human reading the eventual
        recommendation) knows why this strategy passed.
        """

        return StrategySignal(
            symbol=context.symbol,
            timeframe=context.primary_timeframe,
            direction=SignalDirection.NONE,
            confidence=0.0,
            strength=SignalStrength.WEAK,
            strategy_name=self.name,
            reasons=[reason],
            timestamp=context.latest_candle.timestamp,
        )

    @staticmethod
    def _strength_from_confidence(confidence: float) -> SignalStrength:
        """Map a 0-100 confidence score onto the coarse `SignalStrength`
        label, using one shared scale across every strategy.
        """

        if confidence >= _STRONG_CONFIDENCE_THRESHOLD:
            return SignalStrength.STRONG
        if confidence >= _MODERATE_CONFIDENCE_THRESHOLD:
            return SignalStrength.MODERATE
        return SignalStrength.WEAK
