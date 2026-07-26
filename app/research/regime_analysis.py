"""Regime Analysis: classifies market regimes (trending, range, volatile,
low-liquidity, breakout, reversal) from candle/indicator history.

A deterministic, rule-based classifier — distinct from whatever
`app.ai_agents.agents.market_regime_agent` eventually returns (an LLM's
judgment). The two are meant to sometimes disagree; that disagreement is
itself useful signal, not something to reconcile.

These are heuristic, tunable thresholds grounded in common technical-
analysis convention (e.g. ADX >= 25 as "trending" is Welles Wilder's own
textbook threshold), not a provably "correct" classification the way
`statistical_analysis.py`'s PCA/cointegration are exact computations —
tune `RegimeThresholds` per instrument/timeframe as needed.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.domain.exceptions.research import ResearchError
from app.indicators.schemas import ADXPoint, BollingerBandsPoint, IndicatorResult, SingleValuePoint
from app.market.dto import MarketCandle
from app.research.models import MarketRegimeLabel, RegimeAnalysisResult, RegimePeriod


@dataclass(frozen=True, slots=True)
class RegimeThresholds:
    """Tunable thresholds for `classify_regimes`."""

    #: ADX at or above this level counts as a sustained trend (Wilder's
    #: own textbook threshold).
    adx_trend_threshold: float = 25.0

    #: ATR as a percentage of close at or above this level counts as
    #: elevated volatility.
    high_volatility_atr_percent: float = 1.5

    #: Volume below this fraction of its own rolling average counts as
    #: thin liquidity.
    low_liquidity_volume_ratio: float = 0.5

    #: Rolling window for the volume average `low_liquidity_volume_ratio`
    #: compares against.
    volume_lookback_bars: int = 20

    #: A Bollinger bandwidth at or below this quantile of its own rolling
    #: history counts as a "squeeze" — the precondition for `BREAKOUT`.
    breakout_bandwidth_quantile: float = 0.2

    #: Rolling window `breakout_bandwidth_quantile` is computed over.
    bandwidth_lookback_bars: int = 20


def classify_regimes(
    candles: Sequence[MarketCandle],
    adx: IndicatorResult[ADXPoint],
    atr: IndicatorResult[SingleValuePoint],
    bollinger_bands: IndicatorResult[BollingerBandsPoint],
    thresholds: RegimeThresholds | None = None,
) -> RegimeAnalysisResult:
    """Classify each bar into one `MarketRegimeLabel`, then collapse
    consecutive same-regime bars into `RegimePeriod`s.

    Priority order (first match wins, evaluated per bar):
        1. `LOW_LIQUIDITY` — volume too thin to trust any other signal.
        2. `BREAKOUT` — the prior bar was in a bandwidth "squeeze" and
           this bar's close broke outside the prior bar's bands.
        3. `REVERSAL` — the prior bar was trending and the +DI/-DI sign
           just flipped.
        4. `TRENDING` — ADX at or above `adx_trend_threshold`.
        5. `VOLATILE` — ATR% at or above `high_volatility_atr_percent`.
        6. `RANGE` — none of the above.

    Bars during any indicator's warm-up period (a required value is
    still `None`) are skipped entirely — there is no "unknown regime"
    label to assign them, and guessing one would be dishonest.

    Raises:
        ResearchError: fewer than 2 candles, or no bar has every
            required indicator value available to classify.
    """

    if len(candles) < 2:
        raise ResearchError("regime classification requires at least 2 candles")

    resolved_thresholds = thresholds or RegimeThresholds()
    symbol = candles[0].symbol
    bar_interval = pd.Timestamp(candles[1].timestamp) - pd.Timestamp(candles[0].timestamp)

    frame = _build_frame(candles, adx, atr, bollinger_bands, resolved_thresholds)
    labels = _classify_rows(frame, resolved_thresholds)
    if not labels:
        raise ResearchError("no bar has every required indicator value available to classify")

    periods = _collapse_to_periods(labels, bar_interval)
    frequency = _regime_frequency(labels)

    return RegimeAnalysisResult(symbol=symbol, periods=periods, regime_frequency_percent=frequency)


def _build_frame(
    candles: Sequence[MarketCandle],
    adx: IndicatorResult[ADXPoint],
    atr: IndicatorResult[SingleValuePoint],
    bollinger_bands: IndicatorResult[BollingerBandsPoint],
    thresholds: RegimeThresholds,
) -> pd.DataFrame:
    """Align candles and every indicator onto one timestamp index."""

    index = pd.DatetimeIndex([candle.timestamp for candle in candles])
    closes = pd.Series([candle.close for candle in candles], index=index)
    volumes = pd.Series([candle.volume for candle in candles], index=index)

    adx_index = pd.DatetimeIndex([point.timestamp for point in adx.values])
    adx_series = pd.Series([point.adx for point in adx.values], index=adx_index).reindex(index)
    plus_di = pd.Series([point.plus_di for point in adx.values], index=adx_index).reindex(index)
    minus_di = pd.Series([point.minus_di for point in adx.values], index=adx_index).reindex(index)

    atr_index = pd.DatetimeIndex([point.timestamp for point in atr.values])
    atr_series = pd.Series([point.value for point in atr.values], index=atr_index).reindex(index)

    bb_index = pd.DatetimeIndex([point.timestamp for point in bollinger_bands.values])
    lower = pd.Series([point.lower for point in bollinger_bands.values], index=bb_index).reindex(
        index
    )
    upper = pd.Series([point.upper for point in bollinger_bands.values], index=bb_index).reindex(
        index
    )
    bandwidth = pd.Series(
        [point.bandwidth for point in bollinger_bands.values], index=bb_index
    ).reindex(index)

    frame = pd.DataFrame(
        {
            "close": closes,
            "volume": volumes,
            "adx": adx_series,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "atr_percent": 100 * atr_series / closes,
            "lower": lower,
            "upper": upper,
            "bandwidth": bandwidth,
            "rolling_avg_volume": volumes.rolling(thresholds.volume_lookback_bars).mean(),
            "bandwidth_threshold": bandwidth.rolling(thresholds.bandwidth_lookback_bars).quantile(
                thresholds.breakout_bandwidth_quantile
            ),
        }
    )
    return frame


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _classify_rows(
    frame: pd.DataFrame, thresholds: RegimeThresholds
) -> list[tuple[Any, MarketRegimeLabel]]:
    """Compute each bar's regime, dropping bars missing a required
    (possibly shifted-from-the-prior-bar) value.
    """

    working = frame.copy()
    working["di_sign"] = (working["plus_di"] - working["minus_di"]).apply(_sign)
    working["prev_adx"] = working["adx"].shift(1)
    working["prev_di_sign"] = working["di_sign"].shift(1)
    working["prev_lower"] = working["lower"].shift(1)
    working["prev_upper"] = working["upper"].shift(1)
    working["prev_bandwidth"] = working["bandwidth"].shift(1)
    working["prev_bandwidth_threshold"] = working["bandwidth_threshold"].shift(1)
    working = working.dropna()

    return [(row.Index, _classify_row(row, thresholds)) for row in working.itertuples()]


def _classify_row(row: Any, thresholds: RegimeThresholds) -> MarketRegimeLabel:
    if row.volume < thresholds.low_liquidity_volume_ratio * row.rolling_avg_volume:
        return MarketRegimeLabel.LOW_LIQUIDITY

    was_squeezed = row.prev_bandwidth <= row.prev_bandwidth_threshold
    broke_out = row.close > row.prev_upper or row.close < row.prev_lower
    if was_squeezed and broke_out:
        return MarketRegimeLabel.BREAKOUT

    was_trending = row.prev_adx >= thresholds.adx_trend_threshold
    di_flipped = row.di_sign != row.prev_di_sign and row.di_sign != 0 and row.prev_di_sign != 0
    if was_trending and di_flipped:
        return MarketRegimeLabel.REVERSAL

    if row.adx >= thresholds.adx_trend_threshold:
        return MarketRegimeLabel.TRENDING

    if row.atr_percent >= thresholds.high_volatility_atr_percent:
        return MarketRegimeLabel.VOLATILE

    return MarketRegimeLabel.RANGE


def _collapse_to_periods(
    labels: list[tuple[Any, MarketRegimeLabel]], bar_interval: pd.Timedelta
) -> list[RegimePeriod]:
    """Collapse consecutive same-regime bars into `RegimePeriod`s.

    Each period's `end` is its last bar's timestamp plus one bar
    interval — the half-open convention this project's OHLCV data
    already follows (a candle's own timestamp is its *open* time, per
    `MarketCandle`), so even a single-bar regime has `start < end`.
    """

    periods: list[RegimePeriod] = []
    current_label = labels[0][1]
    current_start = labels[0][0]
    last_timestamp = labels[0][0]

    for timestamp, label in labels[1:]:
        if label != current_label:
            periods.append(
                RegimePeriod(
                    regime=current_label, start=current_start, end=last_timestamp + bar_interval
                )
            )
            current_label = label
            current_start = timestamp
        last_timestamp = timestamp

    periods.append(
        RegimePeriod(regime=current_label, start=current_start, end=last_timestamp + bar_interval)
    )
    return periods


def _regime_frequency(
    labels: list[tuple[Any, MarketRegimeLabel]],
) -> dict[MarketRegimeLabel, float]:
    total = len(labels)
    counts: dict[MarketRegimeLabel, int] = {}
    for _, label in labels:
        counts[label] = counts.get(label, 0) + 1
    return {label: 100.0 * count / total for label, count in counts.items()}
