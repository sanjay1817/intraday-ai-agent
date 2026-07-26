"""RSI/MACD Reversal Strategy: RSI crossing back out of an
overbought/oversold extreme, confirmed by MACD histogram momentum.

RSI alone is a poor reversal signal on its own — a strongly-trending
market can stay "oversold" or "overbought" for a long time without
actually reversing. Requiring the *crossover* (RSI just left the
extreme zone, not merely being inside it) plus MACD histogram agreement
(actual momentum, not just an oscillator reading) is the standard fix
for that false-signal case.
"""

from collections.abc import Sequence

from app.indicators.engine import IndicatorRequest
from app.strategy.base_strategy import BaseStrategy
from app.strategy.dto import StrategyContext
from app.strategy.models import SignalDirection, StrategySignal

_RSI_ALIAS = "RSI"
_MACD_ALIAS = "MACD"

_RSI_LENGTH = 14
_MACD_FAST = 12
_MACD_SLOW = 26
_MACD_SIGNAL = 9

_OVERSOLD_THRESHOLD = 30.0
_OVERBOUGHT_THRESHOLD = 70.0

_TARGET_1_R_MULTIPLE = 1.5
_TARGET_2_R_MULTIPLE = 2.5

#: How many recent candles' low/high define the swing stop for a
#: reversal trade — recent enough to be a meaningful nearby swing point,
#: not so short that a single noisy bar sets the stop.
_SWING_LOOKBACK_BARS = 5


class RSIMACDReversalStrategy(BaseStrategy):
    """Reversal: trades RSI leaving an extreme, not merely reaching one."""

    @property
    def name(self) -> str:
        return "RSI_MACD_REVERSAL"

    @property
    def required_indicators(self) -> Sequence[IndicatorRequest]:
        return [
            IndicatorRequest(name="RSI", params={"length": _RSI_LENGTH}, alias=_RSI_ALIAS),
            IndicatorRequest(
                name="MACD",
                params={"fast": _MACD_FAST, "slow": _MACD_SLOW, "signal": _MACD_SIGNAL},
                alias=_MACD_ALIAS,
            ),
        ]

    def analyze(self, context: StrategyContext) -> StrategySignal:
        snapshot = context.primary_snapshot
        close = context.latest_candle.close

        rsi_values = snapshot.indicators[_RSI_ALIAS].values
        macd_values = snapshot.indicators[_MACD_ALIAS].values

        if len(rsi_values) < 2:
            return self._no_signal(context, "Not enough history to detect an RSI crossover.")

        previous_rsi = rsi_values[-2].value
        current_rsi = rsi_values[-1].value
        histogram = macd_values[-1].histogram

        if previous_rsi is None or current_rsi is None or histogram is None:
            return self._no_signal(context, "Indicators still warming up (insufficient history).")

        crossed_up_from_oversold = previous_rsi < _OVERSOLD_THRESHOLD <= current_rsi
        crossed_down_from_overbought = previous_rsi > _OVERBOUGHT_THRESHOLD >= current_rsi

        if crossed_up_from_oversold and histogram > 0:
            direction = SignalDirection.BULLISH
        elif crossed_down_from_overbought and histogram < 0:
            direction = SignalDirection.BEARISH
        else:
            return self._no_signal(
                context, "No RSI extreme-crossover with agreeing MACD histogram momentum."
            )

        recent_candles = snapshot.candles[-_SWING_LOOKBACK_BARS:]
        if direction is SignalDirection.BULLISH:
            stop_loss = min(candle.low for candle in recent_candles)
        else:
            stop_loss = max(candle.high for candle in recent_candles)

        if direction is SignalDirection.BULLISH and stop_loss >= close:
            return self._no_signal(context, "Recent swing low is not below the current price.")
        if direction is SignalDirection.BEARISH and stop_loss <= close:
            return self._no_signal(context, "Recent swing high is not above the current price.")

        risk = abs(close - stop_loss)
        sign = 1 if direction is SignalDirection.BULLISH else -1
        targets = sorted(
            [
                close + sign * risk * _TARGET_1_R_MULTIPLE,
                close + sign * risk * _TARGET_2_R_MULTIPLE,
            ],
            reverse=direction is SignalDirection.BEARISH,
        )

        rsi_extremity = (
            max(0.0, _OVERSOLD_THRESHOLD - previous_rsi)
            if direction is SignalDirection.BULLISH
            else max(0.0, previous_rsi - _OVERBOUGHT_THRESHOLD)
        )
        rsi_component = min(rsi_extremity * 5.0, 50.0)
        histogram_percent_of_price = abs(histogram) / close * 100
        histogram_component = min(histogram_percent_of_price * 100.0, 50.0)
        confidence = min(rsi_component + histogram_component, 100.0)

        reasons = [
            f"RSI({_RSI_LENGTH}) crossed {'up through' if direction is SignalDirection.BULLISH else 'down through'} "
            f"{_OVERSOLD_THRESHOLD:.0f}/{_OVERBOUGHT_THRESHOLD:.0f} "
            f"({previous_rsi:.1f} -> {current_rsi:.1f})",
            f"MACD histogram ({histogram:.4f}) agrees with the reversal direction",
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
