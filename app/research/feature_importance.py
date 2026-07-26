"""Feature Importance: model-agnostic permutation importance, plus
aggregating SHAP values (see `explainability.py`) into a global ranking.

Permutation importance needs no ML library beyond what's already a core
dependency (NumPy/pandas) — it works with *any* prediction function by
construction, unlike SHAP/LIME (`explainability.py`), which each need
their own specific library.
"""

from collections.abc import Callable
from datetime import UTC, datetime

import numpy as np
import numpy.typing as npt
import pandas as pd

from app.domain.exceptions.research import ResearchError
from app.research.models import FeatureImportanceResult, FeatureImportanceScore

#: Runs a fitted model against a feature matrix, returning predictions.
PredictFunction = Callable[[pd.DataFrame], npt.NDArray[np.float64]]

#: Compares true labels to predictions and returns a single score where
#: *higher is better* (e.g. accuracy, R², AUC) — the same convention
#: `sklearn.inspection.permutation_importance` uses. For a "lower is
#: better" metric (e.g. MSE), pass a scoring function that negates it.
ScoringFunction = Callable[[npt.NDArray[np.float64], npt.NDArray[np.float64]], float]


def compute_permutation_importance(
    predict: PredictFunction,
    features: pd.DataFrame,
    labels: npt.NDArray[np.float64],
    scoring: ScoringFunction,
    *,
    n_repeats: int = 5,
    seed: int | None = None,
) -> FeatureImportanceResult:
    """Permutation importance: how much `scoring` degrades when one
    feature column is randomly shuffled, breaking its relationship with
    `labels` while preserving that column's own marginal distribution.

    Args:
        predict: Runs the (already-fitted) model against a feature matrix.
        features: The evaluation set — columns are named features.
        labels: True labels/targets aligned with `features`' rows.
        scoring: Higher-is-better score comparing `labels` to predictions.
        n_repeats: How many independent shuffles to average per feature
            (shuffling is random, so one repeat alone is noisy).
        seed: Reproducibility seed.

    Raises:
        ResearchError: `features` has no columns, or `n_repeats` isn't positive.
    """

    if features.shape[1] == 0:
        raise ResearchError("permutation importance requires at least one feature column")
    if n_repeats <= 0:
        raise ResearchError("n_repeats must be positive")

    rng = np.random.default_rng(seed)
    baseline_score = scoring(labels, predict(features))

    raw_importances: dict[str, float] = {}
    for column in features.columns:
        degradations = np.empty(n_repeats)
        for repeat in range(n_repeats):
            permuted = features.copy()
            permuted[column] = rng.permutation(permuted[column].to_numpy())
            degradations[repeat] = baseline_score - scoring(labels, predict(permuted))
        raw_importances[column] = float(degradations.mean())

    return _to_result("permutation", raw_importances)


def compute_importance_from_shap(shap_values: pd.DataFrame) -> FeatureImportanceResult:
    """Aggregate a SHAP values matrix — `explainability.explain_with_shap`'s
    per-row, per-feature output — into a global importance ranking: the
    mean absolute SHAP value per feature.

    Raises:
        ResearchError: `shap_values` has no columns.
    """

    if shap_values.shape[1] == 0:
        raise ResearchError("SHAP importance requires at least one feature column")

    mean_absolute = shap_values.abs().mean()
    return _to_result(
        "shap_mean_abs", {str(name): float(value) for name, value in mean_absolute.items()}
    )


def _to_result(method: str, raw_importances: dict[str, float]) -> FeatureImportanceResult:
    ranked = sorted(raw_importances.items(), key=lambda item: item[1], reverse=True)
    scores = [
        FeatureImportanceScore(feature_name=name, importance=importance, rank=rank)
        for rank, (name, importance) in enumerate(ranked, start=1)
    ]
    return FeatureImportanceResult(method=method, scores=scores, computed_at=datetime.now(UTC))
