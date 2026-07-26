"""Paper Trading Engine.

Simulates a real exchange — order matching (MARKET/LIMIT/STOP/
STOP_LIMIT/TRAILING_STOP, bracket/OCO legs), FIFO position tracking,
cash/equity/P&L accounting, and trade history — entirely in memory,
against real market data. Never places a real order against a broker.

- `models.py` — the frozen state vocabulary (`PaperOrder`, `Fill`,
  `PaperPosition`, `ClosedTrade`, `TradeMetadata`, `Portfolio`).
- `portfolio.py` — `PortfolioManager`: cash, reserved cash, and
  realized P&L; composes `Portfolio` snapshots.
- `position_manager.py` — `PositionManager`: FIFO lot tracking per
  symbol, weighted-average `PaperPosition`s, realized P&L on close.
- `order_manager.py` — `OrderManager`: order lifecycle, matching,
  bracket/OCO spawning, and settlement — the orchestrator tying the
  two managers above together on every fill.
- `engine.py` — `PaperTradingEngine`: the package's public facade and
  the one thing everything outside it depends on.
- `broker.py` — `PaperBroker`, a full `app.brokers.BrokerInterface`
  implementation backed by `PaperTradingEngine`, so
  `app.brokers.factory.get_broker_adapter` can hand one back for
  `BrokerName.PAPER` and the rest of the application switches between
  paper and a real broker with configuration alone.

Storage is entirely in-memory, by design, for this milestone — no
PostgreSQL/Redis/persistence layer is used or assumed.
"""
