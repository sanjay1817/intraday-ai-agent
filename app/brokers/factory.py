"""Broker adapter factory.

The rest of the application should depend on `BrokerInterface` and this
factory function — never import a concrete adapter class directly — so
adding a fourth broker later means adding one branch here, not touching
every call site.
"""

from app.brokers.angel_one import AngelOneAdapter
from app.brokers.base import BrokerInterface
from app.brokers.upstox import UpstoxAdapter
from app.brokers.zerodha import ZerodhaAdapter
from app.config.settings import Settings
from app.domain.enums.trading import BrokerName
from app.paper.broker import PaperBroker


def get_broker_adapter(broker_name: BrokerName, settings: Settings) -> BrokerInterface:
    """Construct the `BrokerInterface` adapter for `broker_name`.

    Args:
        broker_name: Which broker to connect to.
        settings: Application settings; `settings.broker` supplies both
            the per-broker credentials and the shared HTTP timeout/retry
            configuration every adapter is built with.

    Raises:
        ValueError: `broker_name` has no registered adapter.
    """

    broker_settings = settings.broker

    if broker_name is BrokerName.ANGEL_ONE:
        return AngelOneAdapter(broker_settings.angel_one, broker_settings)
    if broker_name is BrokerName.UPSTOX:
        return UpstoxAdapter(broker_settings.upstox, broker_settings)
    if broker_name is BrokerName.ZERODHA:
        return ZerodhaAdapter(broker_settings.zerodha, broker_settings)
    if broker_name is BrokerName.PAPER:
        # `settings.paper_market_data_broker` can never itself be PAPER
        # (enforced by Settings' own validator), so this recursion
        # always terminates in exactly one more call.
        market_data_broker = get_broker_adapter(settings.paper_market_data_broker, settings)
        return PaperBroker(market_data_broker)

    raise ValueError(f"No broker adapter registered for {broker_name!r}")
