"""Backtest Result Storage: persists each completed run's full
`BacktestResult` to disk as JSON.

No database exists anywhere in this codebase yet (`app.database`/
`app.models`/`app.repositories` are all empty stubs — see the
architecture notes) — introducing SQLAlchemy/Alembic for this one
feature would be a disproportionate amount of new infrastructure for a
single-user, locally-run bot. This instead follows the same file-based
convention the dated trade log (`app.core.logging`) already established:
one JSON file per run under `settings.backtest_data_dir/results/`,
listable and loadable by `run_id`.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from app.backtest.dto import AggregateBacktestResult, BacktestResult

logger = structlog.get_logger(__name__)


class BacktestResultStore:
    def __init__(self, results_dir: Path) -> None:
        self._results_dir = results_dir
        self._results_dir.mkdir(parents=True, exist_ok=True)

    def save(self, result: BacktestResult) -> None:
        path = self._results_dir / f"{result.run_id}.json"
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")

    def save_aggregate(self, result: AggregateBacktestResult) -> None:
        path = self._results_dir / f"{result.run_id}.json"
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")

    def load(self, run_id: str) -> BacktestResult | None:
        path = self._results_dir / f"{run_id}.json"
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if "aggregate" in raw:
            return None  # this run_id belongs to an aggregate result, not a single one
        return BacktestResult.model_validate(raw)

    def load_aggregate(self, run_id: str) -> AggregateBacktestResult | None:
        path = self._results_dir / f"{run_id}.json"
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if "aggregate" not in raw:
            return None
        return AggregateBacktestResult.model_validate(raw)

    def list_run_ids(self) -> list[str]:
        return sorted(path.stem for path in self._results_dir.glob("*.json"))
