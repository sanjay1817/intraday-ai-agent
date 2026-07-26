"""Unit tests for `app.core.logging`."""

import io
import json
from typing import Any

import pytest
import structlog

from app.config.settings import Settings
from app.core.logging import clear_log_buffer, configure_logging, get_recent_logs


def _configured_logger(stream: io.StringIO, **settings_kwargs: object) -> Any:
    settings = Settings(_env_file=None, **settings_kwargs)  # type: ignore[arg-type]
    configure_logging(settings, stream=stream)
    return structlog.get_logger("test.logger")


def test_json_mode_emits_parseable_json_with_expected_keys() -> None:
    stream = io.StringIO()
    logger = _configured_logger(stream, log_level="INFO", log_json=True)

    logger.info("something_happened", foo="bar")

    payload = json.loads(stream.getvalue().strip())
    assert payload["event"] == "something_happened"
    assert payload["foo"] == "bar"
    assert payload["level"] == "info"
    assert "timestamp" in payload


def test_console_mode_emits_human_readable_non_json() -> None:
    stream = io.StringIO()
    logger = _configured_logger(stream, log_level="INFO", log_json=False)

    logger.info("something_happened", foo="bar")

    output = stream.getvalue().strip()
    with pytest.raises(json.JSONDecodeError):
        json.loads(output)
    assert "something_happened" in output
    assert "foo" in output


def test_messages_below_configured_level_are_suppressed() -> None:
    stream = io.StringIO()
    logger = _configured_logger(stream, log_level="INFO", log_json=True)

    logger.debug("should_not_appear")

    assert stream.getvalue() == ""


def test_messages_at_configured_level_are_emitted() -> None:
    stream = io.StringIO()
    logger = _configured_logger(stream, log_level="DEBUG", log_json=True)

    logger.debug("should_appear")

    assert "should_appear" in stream.getvalue()


def test_reconfiguring_does_not_duplicate_output() -> None:
    stream = io.StringIO()
    settings = Settings(_env_file=None, log_level="INFO", log_json=True)

    configure_logging(settings, stream=stream)
    configure_logging(settings, stream=stream)
    structlog.get_logger("test.logger").info("once_only")

    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1


def test_unknown_log_level_raises_value_error() -> None:
    settings = Settings(_env_file=None, log_level="NOT_A_LEVEL")

    with pytest.raises(ValueError, match="unknown log_level"):
        configure_logging(settings)


# -- ring buffer (GET /api/v1/logs) --------------------------------------------------------


def test_recent_logs_capture_level_logger_and_message() -> None:
    clear_log_buffer()
    logger = _configured_logger(io.StringIO(), log_level="INFO", log_json=True)

    logger.info("something_happened", foo="bar")

    entries = get_recent_logs()
    assert len(entries) == 1
    assert entries[0].level == "INFO"
    assert entries[0].logger == "test.logger"
    assert "something_happened" in entries[0].message
    assert "foo" in entries[0].message


def test_recent_logs_render_without_ansi_color_regardless_of_json_setting() -> None:
    clear_log_buffer()
    logger = _configured_logger(io.StringIO(), log_level="INFO", log_json=False)

    logger.info("colorful_in_console")

    entries = get_recent_logs()
    assert "\x1b[" not in entries[0].message  # no ANSI escape codes


def test_recent_logs_below_configured_level_are_not_captured() -> None:
    clear_log_buffer()
    logger = _configured_logger(io.StringIO(), log_level="INFO", log_json=True)

    logger.debug("should_not_appear")

    assert get_recent_logs() == []


def test_recent_logs_respects_limit_keeping_the_newest() -> None:
    clear_log_buffer()
    logger = _configured_logger(io.StringIO(), log_level="INFO", log_json=True)

    for i in range(5):
        logger.info(f"event_{i}")

    entries = get_recent_logs(limit=2)
    assert len(entries) == 2
    assert "event_3" in entries[0].message
    assert "event_4" in entries[1].message


def test_clear_log_buffer_empties_it() -> None:
    logger = _configured_logger(io.StringIO(), log_level="INFO", log_json=True)
    logger.info("will_be_cleared")
    assert get_recent_logs() != []

    clear_log_buffer()

    assert get_recent_logs() == []
