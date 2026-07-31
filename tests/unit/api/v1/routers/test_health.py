"""Unit tests for `app.api.v1.routers.health`."""

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routers.health import router
from app.config.settings import Settings, get_settings


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, app_name="Test Trading Agent", app_env="testing"
    )
    return TestClient(app)


def test_liveness_always_returns_200() -> None:
    response = _client().get("/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_returns_503_when_app_state_has_no_ready_flag() -> None:
    response = _client().get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_readiness_returns_503_when_explicitly_not_ready() -> None:
    client = _client()
    client.app.state.ready = False  # type: ignore[attr-defined]

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_readiness_returns_200_when_ready() -> None:
    client = _client()
    client.app.state.ready = True  # type: ignore[attr-defined]

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_health_reports_settings_derived_identity() -> None:
    response = _client().get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app_name"] == "Test Trading Agent"
    assert body["app_env"] == "testing"
    assert body["ready"] is False
    assert body["uptime_seconds"] >= 0
    assert body["trading_mode"] == "EQUITY"
    assert body["default_broker"] == "angel_one"
    assert body["options_infrastructure_available"] is False
    assert isinstance(body["option_underlyings"], list)
    assert body["option_default_lot_size"] > 0


def test_health_reflects_ready_state() -> None:
    client = _client()
    client.app.state.ready = True  # type: ignore[attr-defined]

    response = client.get("/health")

    assert response.json()["ready"] is True


def test_health_reports_options_infrastructure_available_when_configured() -> None:
    client = _client()
    client.app.state.option_chain_service = object()  # type: ignore[attr-defined]

    response = client.get("/health")

    assert response.json()["options_infrastructure_available"] is True


def test_health_reports_options_infrastructure_unavailable_by_default() -> None:
    response = _client().get("/health")

    assert response.json()["options_infrastructure_available"] is False


def test_health_uptime_increases_monotonically_across_calls() -> None:
    client = _client()

    first = client.get("/health").json()["uptime_seconds"]
    time.sleep(0.01)
    second = client.get("/health").json()["uptime_seconds"]

    assert second > first
