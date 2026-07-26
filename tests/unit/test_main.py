"""Unit and integration tests for `app.main`."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pytest_mock import MockerFixture

from app.config.settings import get_settings
from app.domain.exceptions import BrokerError, IndicatorError, ResearchError
from app.main import create_app, lifespan


def test_create_app_initializes_ready_state_to_false() -> None:
    app = create_app()

    assert app.state.ready is False


def test_create_app_mounts_health_routes() -> None:
    app = create_app()

    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]

    assert {"/live", "/ready", "/health"} <= paths


def test_create_app_registers_domain_exception_handlers() -> None:
    app = create_app()

    assert BrokerError in app.exception_handlers
    assert IndicatorError in app.exception_handlers
    assert ResearchError in app.exception_handlers
    assert Exception in app.exception_handlers


def test_lifespan_sets_ready_true_on_startup_and_false_on_shutdown() -> None:
    app = create_app()
    assert app.state.ready is False

    with TestClient(app):
        assert app.state.ready is True

    assert app.state.ready is False


def test_lifespan_is_idempotent_across_repeated_enter_exit() -> None:
    app = create_app()

    for _ in range(3):
        with TestClient(app):
            assert app.state.ready is True
        assert app.state.ready is False


def test_lifespan_configures_logging_with_loaded_settings(mocker: MockerFixture) -> None:
    configure_logging_spy = mocker.patch("app.main.configure_logging")
    app = create_app()

    with TestClient(app):
        pass

    configure_logging_spy.assert_called_once()
    (settings_arg,) = configure_logging_spy.call_args.args
    assert settings_arg.app_name


def test_invalid_environment_configuration_prevents_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("APP_ENV", "not_a_real_environment")

    try:
        app = create_app()
        with pytest.raises(ValidationError), TestClient(app):
            pass
    finally:
        monkeypatch.delenv("APP_ENV", raising=False)
        get_settings.cache_clear()


def test_end_to_end_live_ready_health_via_running_app() -> None:
    """The exact success criteria from the milestone: with the app
    running (lifespan active, matching what `uvicorn app.main:app`
    does), `/live`, `/ready`, and `/health` all behave correctly.
    """

    with TestClient(create_app()) as client:
        live = client.get("/live")
        assert live.status_code == 200
        assert live.json() == {"status": "alive"}

        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready"}

        health = client.get("/health")
        assert health.status_code == 200
        body = health.json()
        assert body["status"] == "ok"
        assert body["ready"] is True
        assert body["uptime_seconds"] >= 0


def test_ready_reports_503_when_lifespan_has_not_run() -> None:
    app = create_app()
    client = TestClient(app)  # no `with` block — lifespan startup never runs

    response = client.get("/ready")

    assert response.status_code == 503


async def test_lifespan_is_directly_usable_as_an_async_context_manager() -> None:
    """`lifespan` itself (not just through `TestClient`) is a valid
    ASGI-style lifespan context manager — proves it's independently
    testable/reusable, not just an opaque callable FastAPI happens to
    accept.
    """

    app = create_app()

    async with lifespan(app):
        assert app.state.ready is True
    assert app.state.ready is False
