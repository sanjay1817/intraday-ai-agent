"""Broker integrations (order placement, market data feeds).

`BrokerInterface` is the Adapter Pattern contract every broker adapter
satisfies; `get_broker_adapter` constructs the right concrete adapter
(`AngelOneAdapter` / `UpstoxAdapter` / `ZerodhaAdapter`) from settings.
Callers outside this package should depend only on `BrokerInterface` and
`get_broker_adapter` — never import a concrete adapter class directly.
"""

from app.brokers.base import BrokerInterface, TickHandler
from app.brokers.factory import get_broker_adapter

__all__ = ["BrokerInterface", "TickHandler", "get_broker_adapter"]
