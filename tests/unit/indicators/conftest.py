"""Shared fixtures for indicator-engine unit tests."""

import numpy as np
import pandas as pd
import pytest

#: Large enough for every indicator's warm-up period — Ichimoku's default
#: `senkou=52` needs the most, so use comfortably more than that.
_BAR_COUNT = 200


@pytest.fixture
def ohlcv_df() -> pd.DataFrame:
    """A deterministic, synthetic 200-bar (1-minute) OHLCV DataFrame with
    an ordered `DatetimeIndex`, seeded for reproducible assertions.
    """

    rng = np.random.default_rng(seed=42)
    index = pd.date_range("2024-01-02 09:15", periods=_BAR_COUNT, freq="1min")
    close = 100 + np.cumsum(rng.normal(0, 0.5, size=_BAR_COUNT))
    high = close + rng.uniform(0.1, 0.5, size=_BAR_COUNT)
    low = close - rng.uniform(0.1, 0.5, size=_BAR_COUNT)
    open_ = close + rng.uniform(-0.3, 0.3, size=_BAR_COUNT)
    volume = rng.integers(100, 5000, size=_BAR_COUNT)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )
