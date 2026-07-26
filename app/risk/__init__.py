"""Risk Management Engine.

The final authority before any trade reaches execution: position sizing,
exposure limits, drawdown/circuit-breaker halts, and compliance rules.
Neither the Strategy Engine (`app.strategy`) nor the AI Decision Engine
(`app.ai`) may bypass it — every `StrategySignal`/`TradingDecision` is
validated here before anything is ever handed to `app.execution`.
"""
