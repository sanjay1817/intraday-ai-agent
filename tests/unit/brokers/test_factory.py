"""Unit tests for the broker adapter factory."""

import pytest

from app.brokers.angel_one import AngelOneAdapter
from app.brokers.factory import get_broker_adapter
from app.brokers.upstox import UpstoxAdapter
from app.brokers.zerodha import ZerodhaAdapter
from app.config.settings import Settings
from app.domain.enums.trading import BrokerName
from app.paper.broker import PaperBroker


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.mark.parametrize(
    ("broker_name", "expected_type"),
    [
        (BrokerName.ANGEL_ONE, AngelOneAdapter),
        (BrokerName.UPSTOX, UpstoxAdapter),
        (BrokerName.ZERODHA, ZerodhaAdapter),
    ],
)
def test_returns_matching_adapter_type(
    settings: Settings, broker_name: BrokerName, expected_type: type
) -> None:
    adapter = get_broker_adapter(broker_name, settings)

    assert isinstance(adapter, expected_type)
    assert adapter.broker_name is broker_name


def test_unknown_broker_raises_value_error(settings: Settings) -> None:
    with pytest.raises(ValueError, match="No broker adapter registered"):
        get_broker_adapter("not-a-real-broker", settings)  # type: ignore[arg-type]


def test_paper_broker_wraps_the_configured_market_data_broker(settings: Settings) -> None:
    adapter = get_broker_adapter(BrokerName.PAPER, settings)

    assert isinstance(adapter, PaperBroker)
    assert isinstance(adapter.market_data_broker, AngelOneAdapter)  # this fixture's default


def test_paper_broker_market_data_source_is_configurable() -> None:
    settings = Settings(_env_file=None, paper_market_data_broker="upstox")

    adapter = get_broker_adapter(BrokerName.PAPER, settings)

    assert isinstance(adapter, PaperBroker)
    assert isinstance(adapter.market_data_broker, UpstoxAdapter)
