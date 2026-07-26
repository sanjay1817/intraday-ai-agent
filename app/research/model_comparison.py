"""Model Comparison: evaluates multiple already-fitted models against
the same test set and metric, and picks the best one.

Takes a plain `predict` callable per model — like every other module in
this package that touches a model (`feature_importance.py`,
`explainability.py`) — so this works with any model type (scikit-learn,
a hand-rolled function, an artifact loaded via `experiment_tracker.py`)
without depending on a specific ML framework's API.
"""

from collections.abc import Callable, Sequence
from typing import NamedTuple

import numpy as np
import numpy.typing as npt
import pandas as pd

from app.domain.exceptions.research import ResearchError
from app.research.models import ModelComparisonResult, ModelPerformanceSummary

PredictFunction = Callable[[pd.DataFrame], npt.NDArray[np.float64]]

#: Compares true labels to predictions and returns a single score where
#: *higher is better* by convention — pass `maximize=False` to
#: `compare_models` for a "lower is better" metric (e.g. MSE) instead of
#: negating the metric yourself.
ScoringFunction = Callable[[npt.NDArray[np.float64], npt.NDArray[np.float64]], float]


class ModelCandidate(NamedTuple):
    """One model to compare.

    `training_duration_seconds` defaults to `0.0` because this module
    evaluates already-fitted models — it doesn't train them, so it has
    no duration to measure unless the caller tracked one itself.
    """

    name: str
    predict: PredictFunction
    training_duration_seconds: float = 0.0


def compare_models(
    candidates: Sequence[ModelCandidate],
    features: pd.DataFrame,
    labels: npt.NDArray[np.float64],
    scoring: ScoringFunction,
    *,
    metric_name: str = "score",
    maximize: bool = True,
) -> ModelComparisonResult:
    """Evaluate every candidate against the same `features`/`labels`
    using `scoring`, and report which one performed best.

    Args:
        candidates: Models to compare.
        features: The shared evaluation set every candidate is scored
            against — comparing models on different data would make the
            comparison meaningless.
        labels: True labels/targets aligned with `features`' rows.
        scoring: Compares `labels` to a candidate's predictions.
        metric_name: What `scoring` measures, recorded on each
            `ModelPerformanceSummary`.
        maximize: Whether a higher score is better — `False` for a
            "lower is better" metric like MSE.

    Raises:
        ResearchError: `candidates` is empty, or two candidates share a name.
    """

    if not candidates:
        raise ResearchError("model comparison requires at least one candidate")

    names = [candidate.name for candidate in candidates]
    if len(set(names)) != len(names):
        raise ResearchError("candidate model names must be unique")

    summaries = [
        ModelPerformanceSummary(
            model_name=candidate.name,
            metric_name=metric_name,
            metric_value=scoring(labels, candidate.predict(features)),
            training_duration_seconds=candidate.training_duration_seconds,
        )
        for candidate in candidates
    ]

    best = (max if maximize else min)(summaries, key=lambda summary: summary.metric_value)

    return ModelComparisonResult(
        candidates=summaries, best_model_name=best.model_name, comparison_metric=metric_name
    )
