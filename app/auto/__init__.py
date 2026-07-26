"""Automatic Paper Trading.

Orchestrates the already-built pieces of this codebase into a
fully-automatic, market-hours-only trading loop — it introduces no new
market-data, strategy, recommendation, or order-execution logic of its
own:

- `app.brokers` (Angel One) for live prices and candles.
- `app.strategy.engine.StrategyEngine` + `app.instructor.recommendation`
  for BUY/SELL/HOLD recommendations.
- `app.paper.engine.PaperTradingEngine` for every order this package
  ever places — real broker orders are never placed.

- `models.py` — `AutoTradingConfig`/`AutoTradingStatus`.
- `risk.py` — `AutoRiskManager`: the day-scoped counters (max open
  positions, max daily trades, max daily loss, cooldown after
  consecutive losses) — a strategy-level gate on top of whatever
  `PortfolioManager` itself already enforces.
- `orchestrator.py` — `AutoTradingOrchestrator`: the polling loop,
  entry/exit decisions, and structured decision logging tying
  everything above together.
"""
