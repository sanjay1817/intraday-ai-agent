"""Experiment Tracking: records research runs (parameters, metrics,
tags, timing) via MLflow or an in-memory fallback, selected by
`ExperimentConfig.tracking_backend`.

Mirrors the Adapter Pattern used throughout this project (`app.brokers`,
`app.ai`): callers depend only on `ExperimentTracker`, never on a
concrete MLflow or in-memory implementation.

Deliberately synchronous, unlike most I/O in this project: MLflow itself
has no async client, and research/backtesting runs are offline, batch
workloads driven from a script or a background job — not the FastAPI
request-handling event loop most of this codebase's async I/O protects.
A caller that does need to run this from an async context can wrap a
call in `asyncio.to_thread` itself; this module doesn't need to be async
internally to support that.
"""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.domain.exceptions.research import ExperimentTrackingError, UnknownRunError
from app.research.dto import ExperimentConfig, ExperimentTrackingBackend
from app.research.models import ExperimentRecord


class ExperimentTracker(ABC):
    """Contract every experiment-tracking backend implements."""

    @abstractmethod
    def start_run(self, config: ExperimentConfig) -> str:
        """Begin a new run under `config.name`, returning its run ID."""

    @abstractmethod
    def log_params(self, run_id: str, params: dict[str, Any]) -> None:
        """Record `params` against `run_id`."""

    @abstractmethod
    def log_metrics(self, run_id: str, metrics: dict[str, float]) -> None:
        """Record `metrics` against `run_id`."""

    @abstractmethod
    def end_run(self, run_id: str) -> ExperimentRecord:
        """Finish `run_id`, returning its full `ExperimentRecord`."""

    @contextmanager
    def run(self, config: ExperimentConfig) -> Iterator[str]:
        """Start a run, yield its ID, and end it on exit — even if the
        block raises — so a run is never left dangling because a caller
        forgot to call `end_run`.
        """

        run_id = self.start_run(config)
        try:
            yield run_id
        finally:
            self.end_run(run_id)


@dataclass
class _RunState:
    """Mutable bookkeeping for one in-progress run."""

    config: ExperimentConfig
    started_at: datetime
    parameters: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    completed_at: datetime | None = None


class InMemoryExperimentTracker(ExperimentTracker):
    """A fully-functional, single-process experiment tracker.

    Not a stand-in for a real backend — just one with no external
    dependency, useful for tests and for research sessions that don't
    need MLflow's persistence/UI.
    """

    def __init__(self) -> None:
        self._runs: dict[str, _RunState] = {}

    def start_run(self, config: ExperimentConfig) -> str:
        run_id = str(uuid.uuid4())
        self._runs[run_id] = _RunState(config=config, started_at=datetime.now(UTC))
        return run_id

    def log_params(self, run_id: str, params: dict[str, Any]) -> None:
        self._require_run(run_id).parameters.update(params)

    def log_metrics(self, run_id: str, metrics: dict[str, float]) -> None:
        self._require_run(run_id).metrics.update(metrics)

    def end_run(self, run_id: str) -> ExperimentRecord:
        state = self._require_run(run_id)
        state.completed_at = datetime.now(UTC)
        return ExperimentRecord(
            experiment_name=state.config.name,
            run_id=run_id,
            parameters=dict(state.parameters),
            metrics=dict(state.metrics),
            tags=dict(state.config.tags),
            started_at=state.started_at,
            completed_at=state.completed_at,
        )

    def _require_run(self, run_id: str) -> _RunState:
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise UnknownRunError(run_id) from exc


class MLflowExperimentTracker(ExperimentTracker):
    """Tracks experiments via MLflow's `MlflowClient` API.

    Uses the explicit-run-ID `MlflowClient` (not MLflow's "fluent" API —
    `mlflow.start_run()`/`mlflow.log_params()`) deliberately: the fluent
    API tracks the "active run" as thread-local state and logs to
    whichever run is currently active, which doesn't compose with this
    module's `ExperimentTracker` contract of always naming the target
    `run_id` explicitly.

    MLflow stores every parameter value as a string internally — a
    `log_params(run_id, {"ema_fast": 9})` call round-trips back from
    `end_run` as `{"ema_fast": "9"}`, not the original `int`. This is a
    real MLflow behavior, not a bug in this wrapper; a caller that needs
    exact type fidelity should use `InMemoryExperimentTracker` instead.
    """

    def __init__(self, tracking_uri: str | None = None) -> None:
        try:
            from mlflow.tracking import MlflowClient
        except ImportError as exc:
            raise ExperimentTrackingError(
                "the MLflow backend requires the optional 'mlflow' package "
                "(install with the 'research' extra) or use "
                "ExperimentTrackingBackend.IN_MEMORY instead"
            ) from exc

        self._client = MlflowClient(tracking_uri=tracking_uri)

    def start_run(self, config: ExperimentConfig) -> str:
        experiment_id = self._resolve_experiment_id(config.name)
        run = self._client.create_run(experiment_id, tags=config.tags)
        run_id: str = run.info.run_id
        return run_id

    def log_params(self, run_id: str, params: dict[str, Any]) -> None:
        for key, value in params.items():
            self._client.log_param(run_id, key, value)

    def log_metrics(self, run_id: str, metrics: dict[str, float]) -> None:
        for key, value in metrics.items():
            self._client.log_metric(run_id, key, value)

    def end_run(self, run_id: str) -> ExperimentRecord:
        self._client.set_terminated(run_id)
        run = self._client.get_run(run_id)
        experiment = self._client.get_experiment(run.info.experiment_id)

        return ExperimentRecord(
            experiment_name=experiment.name,
            run_id=run_id,
            parameters=dict(run.data.params),
            metrics=dict(run.data.metrics),
            tags={
                key: value for key, value in run.data.tags.items() if not key.startswith("mlflow.")
            },
            started_at=_from_epoch_millis(run.info.start_time),
            completed_at=_from_epoch_millis(run.info.end_time) if run.info.end_time else None,
        )

    def _resolve_experiment_id(self, name: str) -> str:
        experiment = self._client.get_experiment_by_name(name)
        if experiment is not None:
            experiment_id: str = experiment.experiment_id
            return experiment_id
        experiment_id = self._client.create_experiment(name)
        return experiment_id


def _from_epoch_millis(epoch_millis: int) -> datetime:
    """Convert one of MLflow's epoch-millisecond timestamps to a
    timezone-aware `datetime`.
    """

    return datetime.fromtimestamp(epoch_millis / 1000, tz=UTC)


def get_experiment_tracker(config: ExperimentConfig) -> ExperimentTracker:
    """Construct the `ExperimentTracker` for `config.tracking_backend`."""

    if config.tracking_backend is ExperimentTrackingBackend.MLFLOW:
        return MLflowExperimentTracker(tracking_uri=config.tracking_uri)
    return InMemoryExperimentTracker()
