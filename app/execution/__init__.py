"""Live Execution Engine.

Safely executes approved trades through supported broker APIs. Never
makes a trading decision — it only executes validated `RiskDecision`
objects (`app.risk.models`), routed through the existing broker layer
(`app.brokers`) via the Adapter Pattern, never a duplicated broker API.
"""
