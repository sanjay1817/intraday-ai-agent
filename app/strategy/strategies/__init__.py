"""Concrete strategy implementations.

Every module here defines one `BaseStrategy` subclass, imported directly
and explicitly by `app.strategy.engine.StrategyEngine` — deliberately
not a `pkgutil`-based auto-discovery registry like `app.indicators`'s.
That pattern earned its complexity there with 11 indicators; at 3
strategies, an explicit list is simpler to read, and a registry can be
introduced later if the strategy count ever grows enough to justify it.
"""
