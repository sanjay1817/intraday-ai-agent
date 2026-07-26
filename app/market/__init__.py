"""Market Data Engine.

Collects, validates, normalizes, caches, and streams market data — ticks,
candles, symbol metadata, and session state. This package never makes a
trading decision and never talks to `app.ai`; it only produces data for
other layers (the AI Decision Engine, the API, the scheduler) to consume.
"""
