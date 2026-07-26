"""Unit tests for `app.api.v1.routers.logs`."""

import structlog
from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.core.logging import clear_log_buffer, configure_logging
from app.main import create_app


def test_get_logs_returns_recent_entries() -> None:
    clear_log_buffer()
    configure_logging(Settings(_env_file=None, log_level="INFO"))
    structlog.get_logger("test.logs.endpoint").info("dashboard_probe_event", foo="bar")

    client = TestClient(create_app())
    response = client.get("/api/v1/logs")

    assert response.status_code == 200
    body = response.json()
    assert len(body) >= 1
    assert any("dashboard_probe_event" in entry["message"] for entry in body)
    entry = next(e for e in body if "dashboard_probe_event" in e["message"])
    assert entry["level"] == "INFO"
    assert entry["logger"] == "test.logs.endpoint"
    assert "timestamp" in entry


def test_get_logs_respects_limit_query_param() -> None:
    clear_log_buffer()
    configure_logging(Settings(_env_file=None, log_level="INFO"))
    logger = structlog.get_logger("test.logs.endpoint")
    for i in range(5):
        logger.info(f"limited_event_{i}")

    client = TestClient(create_app())
    response = client.get("/api/v1/logs", params={"limit": 2})

    assert response.status_code == 200
    assert len(response.json()) == 2


def test_get_logs_rejects_non_positive_limit() -> None:
    client = TestClient(create_app())

    response = client.get("/api/v1/logs", params={"limit": 0})

    assert response.status_code == 422


def test_get_logs_default_limit_is_200() -> None:
    clear_log_buffer()
    configure_logging(Settings(_env_file=None, log_level="INFO"))
    logger = structlog.get_logger("test.logs.endpoint")
    for i in range(250):
        logger.info(f"bulk_event_{i}")

    client = TestClient(create_app())
    response = client.get("/api/v1/logs")

    assert response.status_code == 200
    assert len(response.json()) == 200
