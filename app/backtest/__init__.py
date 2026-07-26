"""Backtesting & Research Framework.

Replays historical market data as if it were arriving live, so the
Strategy Engine, AI Decision Engine, Risk Engine, and Paper Trading
Engine can run unchanged against it. Never calls a live broker API.

Currently implemented: configuration/output models, the simulation
clock, historical data loading/dataset management, tick/candle replay,
and performance metrics — the pieces that don't depend on the other
engines being further along. `strategy_runner.py`/`ai_runner.py`/
`risk_runner.py`/`execution_runner.py` and everything built on top of
them (optimization, walk-forward, Monte Carlo, benchmarking, reporting)
are intentionally not yet started; see project history for why.
"""
