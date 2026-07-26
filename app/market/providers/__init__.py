"""Market-data provider adapters (Angel One, Upstox, Zerodha).

Mirrors the Adapter Pattern used by `app.brokers`: `MarketDataProvider`
(app.market.providers.base) is the contract every concrete provider
satisfies, so `app.market.manager` never depends on a specific broker's
market-data API.
"""
