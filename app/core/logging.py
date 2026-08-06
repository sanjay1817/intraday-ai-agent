"""Structured logging configuration for the whole application.

Every module in this codebase already logs via
`structlog.get_logger(__name__)` (see `app.brokers`, `app.utils.retry`) —
those calls return a lazy proxy that only starts producing real,
formatted output once `configure_logging()` (this module) has run. This
is the one place that decides *how* every log line in the process is
rendered (JSON for log aggregation in staging/production, human-readable
console output for local development) and *whether* a given level is
emitted at all; no other module needs to know or care which mode is
active.

Routes stdlib `logging` through the same structlog processor chain (via
`structlog.stdlib.ProcessorFormatter`) so third-party libraries that log
through the standard `logging` module (httpx, uvicorn, asyncio) render
identically to this project's own structlog call sites, rather than
appearing as a separately-formatted stream.

Also keeps the most recent log lines in an in-memory ring buffer
(`get_recent_logs`) — this project has no log-aggregation system, so
without this, nothing outside the process's own stdout could ever see
recent log history. This is what `GET /api/v1/logs` (the dashboard's
Logs page) reads from.
"""

import logging
import sys
from collections import deque
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TextIO

import structlog
from pydantic import BaseModel, ConfigDict

from app.config.settings import Settings

#: Name of the dedicated stdlib logger that `TRADE_LOGGER_NAME` writes
#: to, kept separate from the root logger's handlers so trade activity
#: lands in its own dated file (see `_DatedFileHandler`) in addition to
#: (via propagation) the normal console/ring-buffer output.
TRADE_LOGGER_NAME = "trades"

_SHARED_PROCESSORS: list[structlog.typing.Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]

#: Bounded so long-running processes don't accumulate log history
#: forever; 2000 lines is generous for a dashboard's "recent activity"
#: view without being a real persistence mechanism.
_LOG_BUFFER_MAXLEN = 2000
_log_buffer: deque["LogEntry"] = deque(maxlen=_LOG_BUFFER_MAXLEN)


class LogEntry(BaseModel):
    """One captured log record — what `GET /api/v1/logs` returns."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    level: str
    logger: str
    message: str


class _RingBufferHandler(logging.Handler):
    """Appends every emitted record (already rendered through the same
    processor chain the main stream handler uses, minus ANSI color) to
    the module-level ring buffer.
    """

    def __init__(self, formatter: logging.Formatter) -> None:
        super().__init__()
        self._render = formatter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self._render.format(record)
        except Exception:  # noqa: BLE001 - a broken record must not crash logging itself
            message = record.getMessage()
        _log_buffer.append(
            LogEntry(
                timestamp=datetime.fromtimestamp(record.created, tz=UTC),
                level=record.levelname,
                logger=record.name,
                message=message,
            )
        )


class _DatedFileHandler(logging.Handler):
    """Writes records to `{log_dir}/trades-YYYY-MM-DD.log`, switching to
    a new file the first time a record is emitted after midnight.

    A plain `TimedRotatingFileHandler` was considered, but it names the
    *current* day's file without a date suffix (only past days get one
    on rollover); this always names the active file by date, so any
    given day's trades live in one predictably-named file from the
    start, whether or not the process is later restarted.
    """

    def __init__(self, log_dir: Path) -> None:
        super().__init__()
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._current_date: date | None = None
        self._file_handler: logging.FileHandler | None = None

    def emit(self, record: logging.LogRecord) -> None:
        today = datetime.now().date()
        if today != self._current_date or self._file_handler is None:
            self._rotate(today)
        assert self._file_handler is not None  # narrowed by _rotate above
        self._file_handler.setFormatter(self.formatter)
        self._file_handler.emit(record)

    def _rotate(self, today: date) -> None:
        if self._file_handler is not None:
            self._file_handler.close()
        path = self._log_dir / f"trades-{today.isoformat()}.log"
        self._file_handler = logging.FileHandler(path, encoding="utf-8")
        self._current_date = today

    def close(self) -> None:
        if self._file_handler is not None:
            self._file_handler.close()
        super().close()


def get_recent_logs(limit: int = 200) -> list[LogEntry]:
    """The most recent captured log entries, oldest first, capped at `limit`."""

    entries = list(_log_buffer)
    return entries[-limit:] if limit < len(entries) else entries


def clear_log_buffer() -> None:
    """Empty the in-memory log buffer.

    Test-only: without this, one test's log output could leak into
    another's `GET /api/v1/logs` assertions, since the buffer is
    module-level (process-wide) state.
    """

    _log_buffer.clear()


def configure_logging(settings: Settings, *, stream: TextIO | None = None) -> None:
    """Configure `structlog` (and the stdlib `logging` it's routed
    through) for the whole process.

    Safe to call more than once — e.g. once per test — since each call
    replaces the previous configuration outright rather than layering a
    new handler on top of it.

    Args:
        settings: Supplies `log_level` (the minimum level emitted) and
            `log_json` (JSON lines vs. human-readable console output).
        stream: Where rendered log lines are written; defaults to
            `sys.stdout`. Overridable so tests can assert on captured
            output without patching global state.

    Raises:
        ValueError: `settings.log_level` isn't a recognized level name.
    """

    output_stream = stream if stream is not None else sys.stdout

    resolved_level = logging.getLevelNamesMapping().get(settings.log_level.upper())
    if resolved_level is None:
        raise ValueError(f"unknown log_level {settings.log_level!r}")

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*_SHARED_PROCESSORS, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )

    handler = logging.StreamHandler(output_stream)
    handler.setFormatter(formatter)

    # Independent of `settings.log_json`: the ring buffer always renders
    # without ANSI color, since `GET /api/v1/logs` serves it to a web
    # dashboard, not a terminal.
    ring_buffer_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
    )
    ring_buffer_handler = _RingBufferHandler(ring_buffer_formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.addHandler(ring_buffer_handler)
    root_logger.setLevel(resolved_level)

    # Dedicated dated trade log: attached only to the "trades" logger (not
    # root), but that logger still propagates up to root so buy/sell
    # entries also show up in the normal console output and the
    # dashboard's Logs page — this handler just additionally captures
    # them, per day, on disk.
    trade_logger = logging.getLogger(TRADE_LOGGER_NAME)
    for existing in list(trade_logger.handlers):
        trade_logger.removeHandler(existing)
        existing.close()
    trade_file_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
    )
    trade_file_handler = _DatedFileHandler(Path(settings.trade_log_dir))
    trade_file_handler.setFormatter(trade_file_formatter)
    trade_logger.addHandler(trade_file_handler)
    trade_logger.setLevel(resolved_level)
    trade_logger.propagate = True
