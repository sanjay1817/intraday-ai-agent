"""Strategy Engine.

Generates deterministic trade setups (`StrategySignal`) from market data
and technical indicators. Never talks to a broker and never makes the
final trading call — the AI Decision Engine (`app.ai`) evaluates only the
setups this package produces.
"""
