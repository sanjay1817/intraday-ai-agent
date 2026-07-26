"""Unit tests for `app.api.v1.routers.auto`.

Every test enters `TestClient` as a context manager
(`with TestClient(app) as client:`) rather than a bare `TestClient(app)`:
`AutoTradingOrchestrator.start()`/`stop()` create event-loop-bound
`asyncio` primitives (`Event`, `Task`), and a bare `TestClient` may run
each request on its own throwaway event loop, which breaks a start-then-
stop call sequence with "bound to a different event loop" — a TestClient
artifact, not a production concern (a real server has exactly one event
loop for its whole lifetime). Entering as a context manager keeps one
event loop alive for every request made through it, matching production.
"""

from unittest.mock import create_autospec

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routers.auto import get_orchestrator
from app.auto.models import AutoTradingConfig
from app.auto.orchestrator import AutoTradingOrchestrator
from app.brokers.base import BrokerInterface
from app.config.settings import get_settings
from app.domain.enums.trading import BrokerName
from app.main import create_app
from app.market.dto import MarketSessionState
from app.paper.engine import PaperTradingEngine


def make_app(**config_overrides: object) -> FastAPI:
    app = create_app()
    # Market is always CLOSED for these tests (see session_provider
    # below), so the loop never runs a cycle and never touches this --
    # a plain autospec with no configured behavior is enough.
    broker = create_autospec(BrokerInterface, instance=True)
    config_defaults: dict[str, object] = {"symbols": ["INFY-EQ"], "poll_interval_seconds": 0.02}
    config_defaults.update(config_overrides)
    orchestrator = AutoTradingOrchestrator(
        broker=broker,
        broker_name=BrokerName.ANGEL_ONE,
        paper_engine=PaperTradingEngine(),
        config=AutoTradingConfig(**config_defaults),  # type: ignore[arg-type]
        # Market always "closed" for these API-level tests -- the loop
        # must start/stop cleanly either way, and no test here needs an
        # actual cycle to run (that's covered by tests/unit/auto/).
        session_provider=lambda: MarketSessionState.CLOSED,
    )
    app.dependency_overrides[get_orchestrator] = lambda: orchestrator
    return app


def test_status_when_never_started() -> None:
    with TestClient(make_app()) as client:
        response = client.get("/api/v1/auto/status")

    assert response.status_code == 200
    body = response.json()
    assert body["running"] is False
    assert body["started_at"] is None
    assert body["symbols"] == ["INFY-EQ"]
    assert body["cycle_count"] == 0
    assert body["open_position_count"] == 0
    assert body["trades_today"] == 0
    assert body["daily_realized_pnl"] == 0.0
    assert body["consecutive_losses"] == 0
    assert body["cooldown_until"] is None
    assert body["last_error"] is None


def test_start_marks_running() -> None:
    with TestClient(make_app()) as client:
        response = client.post("/api/v1/auto/start")

        assert response.status_code == 200
        body = response.json()
        assert body["running"] is True
        assert body["started_at"] is not None

        status_response = client.get("/api/v1/auto/status")
        assert status_response.json()["running"] is True

        client.post("/api/v1/auto/stop")


def test_start_is_idempotent_over_http() -> None:
    with TestClient(make_app()) as client:
        first = client.post("/api/v1/auto/start")
        second = client.post("/api/v1/auto/start")

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["running"] is True

        client.post("/api/v1/auto/stop")


def test_stop_marks_not_running() -> None:
    with TestClient(make_app()) as client:
        client.post("/api/v1/auto/start")

        response = client.post("/api/v1/auto/stop")

        assert response.status_code == 200
        assert response.json()["running"] is False


def test_stop_when_never_started_is_not_an_error() -> None:
    with TestClient(make_app()) as client:
        response = client.post("/api/v1/auto/stop")

    assert response.status_code == 200
    assert response.json()["running"] is False


def test_start_then_stop_then_start_again() -> None:
    with TestClient(make_app()) as client:
        client.post("/api/v1/auto/start")
        client.post("/api/v1/auto/stop")
        response = client.post("/api/v1/auto/start")

        assert response.json()["running"] is True

        client.post("/api/v1/auto/stop")


def test_auto_trading_enabled_setting_starts_automatically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Settings.auto_trading_enabled=True` must start the orchestrator
    during the real lifespan -- this is the one test in this file that
    exercises the *real* `app.state.auto_orchestrator`, not an override.
    """

    get_settings.cache_clear()
    monkeypatch.setenv("AUTO_TRADING_ENABLED", "true")
    monkeypatch.setenv("AUTO_SYMBOLS", "[]")  # nothing for the loop to actually act on
    try:
        with TestClient(create_app()) as client:
            response = client.get("/api/v1/auto/status")
            assert response.json()["running"] is True
    finally:
        get_settings.cache_clear()
