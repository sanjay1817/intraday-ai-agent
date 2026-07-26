"""EMA Trend-Following Strategy: a fast/slow EMA crossover confirmed by
ADX (trend strength) and SuperTrend (trend direction).

A raw EMA crossover alone is a poor intraday signal — it fires
constantly in a ranging market. Requiring ADX above a minimum threshold
filters out exactly that case (ADX measures trend strength, not
direction), and requiring SuperTrend's own direction to agree with the
EMA cross rejects the case where the two disagree (a recent whipsaw,
not an established trend). All three conditions must agree before this
strategy proposes a trade.

The stop-loss is SuperTrend's own active band (`long_band`/`short_band`)
— SuperTrend is itself a trailing-stop indicator, so reusing its band
directly is the standard technique, not an invented one.
"""

from collections.abc import Sequence

from app.indicators.engine import IndicatorRequest
from app.strategy.base_strategy import BaseStrategy
from app.strategy.dto import StrategyContext
from app.strategy.models import SignalDirection, StrategySignal

_EMA_FAST_ALIAS = "EMA_FAST"
_EMA_SLOW_ALIAS = "EMA_SLOW"
_ADX_ALIAS = "ADX"
_SUPERTREND_ALIAS = "SUPERTREND"

_EMA_FAST_LENGTH = 9
_EMA_SLOW_LENGTH = 21
_ADX_LENGTH = 14
_SUPERTREND_LENGTH = 10
_SUPERTREND_MULTIPLIER = 3.0

#: Below this, ADX signals a ranging (non-trending) market — trading an
#: EMA cross here is exactly the false-signal case this strategy exists
#: to reject.
_MIN_ADX_FOR_TREND = 20.0

_TARGET_1_R_MULTIPLE = 1.5
_TARGET_2_R_MULTIPLE = 2.5


class EMATrendStrategy(BaseStrategy):
    """Trend-following: trades in the direction of an established trend,
    not every EMA crossover.
    """

    @property
    def name(self) -> str:
        return "EMA_TREND"

    @property
    def required_indicators(self) -> Sequence[IndicatorRequest]:
        return [
            IndicatorRequest(
                name="EMA", params={"length": _EMA_FAST_LENGTH}, alias=_EMA_FAST_ALIAS
            ),
            IndicatorRequest(
                name="EMA", params={"length": _EMA_SLOW_LENGTH}, alias=_EMA_SLOW_ALIAS
            ),
            IndicatorRequest(name="ADX", params={"length": _ADX_LENGTH}, alias=_ADX_ALIAS),
            IndicatorRequest(
                name="SUPERTREND",
                params={"length": _SUPERTREND_LENGTH, "multiplier": _SUPERTREND_MULTIPLIER},
                alias=_SUPERTREND_ALIAS,
            ),
        ]

    def analyze(self, context: StrategyContext) -> StrategySignal:
        snapshot = context.primary_snapshot
        close = context.latest_candle.close

        ema_fast_point = snapshot.indicators[_EMA_FAST_ALIAS].values[-1]
        ema_slow_point = snapshot.indicators[_EMA_SLOW_ALIAS].values[-1]
        adx_point = snapshot.indicators[_ADX_ALIAS].values[-1]
        supertrend_point = snapshot.indicators[_SUPERTREND_ALIAS].values[-1]

        if (
            ema_fast_point.value is None
            or ema_slow_point.value is None
            or adx_point.adx is None
            or supertrend_point.direction is None
        ):
            return self._no_signal(context, "Indicators still warming up (insufficient history).")

        ema_fast = ema_fast_point.value
        ema_slow = ema_slow_point.value
        adx = adx_point.adx
        supertrend_direction = supertrend_point.direction

        if adx < _MIN_ADX_FOR_TREND:
            return self._no_signal(
                context,
                f"ADX={adx:.1f} below {_MIN_ADX_FOR_TREND:.0f}: no established trend.",
            )

        if ema_fast > ema_slow and supertrend_direction == 1:
            direction = SignalDirection.BULLISH
        elif ema_fast < ema_slow and supertrend_direction == -1:
            direction = SignalDirection.BEARISH
        else:
            return self._no_signal(
                context, "EMA cross and SuperTrend direction disagree: no confirmed trend."
            )

        stop_loss = (
            supertrend_point.long_band
            if direction is SignalDirection.BULLISH
            else supertrend_point.short_band
        )
        if stop_loss is None:
            return self._no_signal(context, "SuperTrend band unavailable for a stop-loss.")
        if direction is SignalDirection.BULLISH and stop_loss >= close:
            return self._no_signal(context, "SuperTrend stop is not below the current price.")
        if direction is SignalDirection.BEARISH and stop_loss <= close:
            return self._no_signal(context, "SuperTrend stop is not above the current price.")

        risk = abs(close - stop_loss)
        sign = 1 if direction is SignalDirection.BULLISH else -1
        targets = sorted(
            [
                close + sign * risk * _TARGET_1_R_MULTIPLE,
                close + sign * risk * _TARGET_2_R_MULTIPLE,
            ],
            reverse=direction is SignalDirection.BEARISH,
        )

        separation_percent = abs(ema_fast - ema_slow) / close * 100
        adx_component = min(adx, 50.0) / 50.0 * 60.0
        separation_component = min(separation_percent * 20.0, 40.0)
        confidence = min(adx_component + separation_component, 100.0)

        trend_word = "uptrend" if direction is SignalDirection.BULLISH else "downtrend"
        reasons = [
            f"EMA({_EMA_FAST_LENGTH}) {'above' if direction is SignalDirection.BULLISH else 'below'} "
            f"EMA({_EMA_SLOW_LENGTH}): {trend_word}",
            f"ADX={adx:.1f} confirms trend strength (>= {_MIN_ADX_FOR_TREND:.0f})",
            f"SuperTrend direction agrees ({'up' if supertrend_direction == 1 else 'down'})",
        ]

        return StrategySignal(
            symbol=context.symbol,
            timeframe=context.primary_timeframe,
            direction=direction,
            entry=close,
            stop_loss=stop_loss,
            targets=targets,
            risk_reward=_TARGET_1_R_MULTIPLE,
            confidence=confidence,
            strength=self._strength_from_confidence(confidence),
            strategy_name=self.name,
            reasons=reasons,
            timestamp=context.latest_candle.timestamp,
        )
