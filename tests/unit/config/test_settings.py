"""Unit tests for `app.config.settings`, focused on `default_broker`."""

import pytest
from pydantic import ValidationError

from app.brokers.angel_one import AngelOneAdapter
from app.brokers.factory import get_broker_adapter
from app.brokers.zerodha import ZerodhaAdapter
from app.config.settings import Settings, get_settings
from app.domain.enums.trading import BrokerName


def test_default_broker_defaults_to_angel_one_when_unset() -> None:
    settings = Settings(_env_file=None)

    assert settings.default_broker is BrokerName.ANGEL_ONE


def test_default_broker_is_overridable_via_constructor() -> None:
    settings = Settings(_env_file=None, default_broker="upstox")

    assert settings.default_broker is BrokerName.UPSTOX


def test_default_broker_is_overridable_via_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("DEFAULT_BROKER", "angel_one")

    try:
        settings = Settings()
        assert settings.default_broker is BrokerName.ANGEL_ONE
    finally:
        monkeypatch.delenv("DEFAULT_BROKER", raising=False)
        get_settings.cache_clear()


def test_unrecognized_default_broker_raises_a_clear_validation_error() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, default_broker="not_a_real_broker")

    message = str(exc_info.value)
    assert "default_broker" in message
    assert "angel_one" in message
    assert "upstox" in message
    assert "zerodha" in message


def test_existing_environment_without_default_broker_still_constructs_successfully() -> None:
    """Backward compatibility: an environment that predates this setting
    (no `DEFAULT_BROKER` anywhere) must keep working unchanged.
    """

    settings = Settings(_env_file=None)

    assert settings.app_name == "Intraday Trading Agent"
    assert settings.default_broker is BrokerName.ANGEL_ONE
    assert settings.broker.zerodha.base_url == "https://api.kite.trade"


@pytest.mark.parametrize(
    ("broker_name", "expected_type"),
    [
        (BrokerName.ZERODHA, ZerodhaAdapter),
        (BrokerName.ANGEL_ONE, AngelOneAdapter),
    ],
)
def test_default_broker_resolves_through_the_existing_factory(
    broker_name: BrokerName, expected_type: type
) -> None:
    settings = Settings(_env_file=None, default_broker=broker_name)

    adapter = get_broker_adapter(settings.default_broker, settings)

    assert isinstance(adapter, expected_type)
    assert adapter.broker_name is broker_name


def test_paper_market_data_broker_defaults_to_angel_one() -> None:
    settings = Settings(_env_file=None)

    assert settings.paper_market_data_broker is BrokerName.ANGEL_ONE


def test_paper_market_data_broker_is_overridable() -> None:
    settings = Settings(_env_file=None, paper_market_data_broker="zerodha")

    assert settings.paper_market_data_broker is BrokerName.ZERODHA


def test_paper_market_data_broker_cannot_be_paper_itself() -> None:
    with pytest.raises(ValidationError, match="paper_market_data_broker"):
        Settings(_env_file=None, paper_market_data_broker="paper")


def test_default_broker_can_be_paper() -> None:
    settings = Settings(_env_file=None, default_broker="paper")

    assert settings.default_broker is BrokerName.PAPER


def test_paper_initial_capital_defaults_to_100000() -> None:
    settings = Settings(_env_file=None)

    assert settings.paper_initial_capital == 100_000.0


def test_paper_initial_capital_is_overridable() -> None:
    settings = Settings(_env_file=None, paper_initial_capital=500_000.0)

    assert settings.paper_initial_capital == 500_000.0


def test_paper_initial_capital_rejects_non_positive_values() -> None:
    with pytest.raises(ValidationError, match="paper_initial_capital"):
        Settings(_env_file=None, paper_initial_capital=0.0)


def test_auto_trading_settings_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.auto_trading_enabled is False
    assert settings.auto_symbols == []
    assert settings.auto_confidence_threshold == 60.0
    assert settings.auto_min_confirmations == 2
    assert settings.max_open_positions == 3
    assert settings.max_daily_trades == 10
    assert settings.max_daily_loss == 5000.0


def test_auto_symbols_parses_a_json_array_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("AUTO_SYMBOLS", '["SBIN-EQ","RELIANCE-EQ"]')

    try:
        settings = Settings()
        assert settings.auto_symbols == ["SBIN-EQ", "RELIANCE-EQ"]
    finally:
        monkeypatch.delenv("AUTO_SYMBOLS", raising=False)
        get_settings.cache_clear()


def test_auto_trading_settings_are_overridable() -> None:
    settings = Settings(
        _env_file=None,
        auto_trading_enabled=True,
        auto_symbols=["SBIN-EQ"],
        auto_confidence_threshold=75.0,
        auto_min_confirmations=3,
        max_open_positions=5,
        max_daily_trades=20,
        max_daily_loss=10_000.0,
    )

    assert settings.auto_trading_enabled is True
    assert settings.auto_symbols == ["SBIN-EQ"]
    assert settings.auto_confidence_threshold == 75.0
    assert settings.auto_min_confirmations == 3
    assert settings.max_open_positions == 5
    assert settings.max_daily_trades == 20
    assert settings.max_daily_loss == 10_000.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_open_positions", 0),
        ("max_daily_trades", 0),
        ("max_daily_loss", 0.0),
    ],
)
def test_auto_trading_numeric_settings_reject_non_positive_values(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError, match=field):
        Settings(_env_file=None, **{field: value})
