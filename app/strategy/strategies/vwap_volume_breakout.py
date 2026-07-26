"""VWAP/Volume Breakout Strategy: price crossing the session VWAP on
above-average volume.

A price crossing VWAP on ordinary volume is routine noise — VWAP is
crossed constantly intraday. Requiring volume meaningfully above its
own recent average at the moment of the cross is what distinguishes a
genuine breakout (real participation behind the move) from an
unremarkable wiggle.

The stop-loss is VWAP itself: if a VWAP breakout fails, price falling
back through VWAP is the standard, direct signal that it has failed —
not an invented rule.
"""

from collections.abc import Sequence

from app.indicators.engine import IndicatorRequest
from app.strategy.base_strategy import BaseStrategy
from app.strategy.dto import StrategyContext
from app.strategy.models import SignalDirection, StrategySignal

_VWAP_ALIAS = "VWAP"
_VOLUME_SMA_ALIAS = "VOLUME_SMA"

_VOLUME_SMA_LENGTH = 20

#: Current volume must be at least this multiple of its recent average
#: for a VWAP cross to count as a breakout rather than routine noise.
_MIN_VOLUME_MULTIPLE = 1.5

_TARGET_1_R_MULTIPLE = 1.5
_TARGET_2_R_MULTIPLE = 2.5


class VWAPVolumeBreakoutStrategy(BaseStrategy):
    """Breakout: trades a VWAP cross only when volume confirms real
    participation behind it.
    """

    @property
    def name(self) -> str:
        return "VWAP_VOLUME_BREAKOUT"

    @property
    def required_indicators(self) -> Sequence[IndicatorRequest]:
        return [
            IndicatorRequest(name="VWAP", alias=_VWAP_ALIAS),
            IndicatorRequest(
                name="VOLUME_SMA", params={"length": _VOLUME_SMA_LENGTH}, alias=_VOLUME_SMA_ALIAS
            ),
        ]

    def analyze(self, context: StrategyContext) -> StrategySignal:
        snapshot = context.primary_snapshot
        candles = snapshot.candles
        close = context.latest_candle.close

        vwap_values = snapshot.indicators[_VWAP_ALIAS].values
        volume_sma_values = snapshot.indicators[_VOLUME_SMA_ALIAS].values

        if len(candles) < 2:
            return self._no_signal(context, "Not enough history to detect a VWAP cross.")

        previous_close = candles[-2].close
        previous_vwap = vwap_values[-2].value
        current_vwap = vwap_values[-1].value
        current_volume_average = volume_sma_values[-1].value

        if previous_vwap is None or current_vwap is None or current_volume_average is None:
            return self._no_signal(context, "Indicators still warming up (insufficient history).")

        current_volume = context.latest_candle.volume
        volume_ratio = (
            current_volume / current_volume_average if current_volume_average > 0 else 0.0
        )

        crossed_above = previous_close <= previous_vwap and close > current_vwap
        crossed_below = previous_close >= previous_vwap and close < current_vwap

        if crossed_above and volume_ratio >= _MIN_VOLUME_MULTIPLE:
            direction = SignalDirection.BULLISH
        elif crossed_below and volume_ratio >= _MIN_VOLUME_MULTIPLE:
            direction = SignalDirection.BEARISH
        else:
            return self._no_signal(
                context,
                f"No VWAP cross with volume >= {_MIN_VOLUME_MULTIPLE:.1f}x its {_VOLUME_SMA_LENGTH}-bar average.",
            )

        stop_loss = current_vwap
        if direction is SignalDirection.BULLISH and stop_loss >= close:
            return self._no_signal(context, "VWAP is not below the current price.")
        if direction is SignalDirection.BEARISH and stop_loss <= close:
            return self._no_signal(context, "VWAP is not above the current price.")

        risk = abs(close - stop_loss)
        sign = 1 if direction is SignalDirection.BULLISH else -1
        targets = sorted(
            [
                close + sign * risk * _TARGET_1_R_MULTIPLE,
                close + sign * risk * _TARGET_2_R_MULTIPLE,
            ],
            reverse=direction is SignalDirection.BEARISH,
        )

        distance_percent = abs(close - current_vwap) / current_vwap * 100
        distance_component = min(distance_percent * 10.0, 50.0)
        volume_component = min((volume_ratio - 1.0) * 25.0, 50.0)
        confidence = min(max(distance_component + volume_component, 0.0), 100.0)

        reasons = [
            f"Price crossed {'above' if direction is SignalDirection.BULLISH else 'below'} "
            f"session VWAP ({previous_close:.2f} -> {close:.2f} vs VWAP {current_vwap:.2f})",
            f"Volume is {volume_ratio:.2f}x its {_VOLUME_SMA_LENGTH}-bar average "
            f"(>= {_MIN_VOLUME_MULTIPLE:.1f}x required)",
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
