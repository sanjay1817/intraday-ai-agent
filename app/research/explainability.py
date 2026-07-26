"""Explainable AI: SHAP and LIME local (per-prediction) explanations.

Both are optional 'research' extras, imported lazily so importing this
module never requires either installed until the corresponding function
is actually called. Global feature importance built from either
method's output lives in `feature_importance.py`
(`compute_importance_from_shap`), not here — this module's job is
per-prediction attribution, not a global ranking.

Both functions take a plain regression `predict` function (one numeric
prediction per row), matching `feature_importance.py`'s `PredictFunction`
— classification support (per-class probabilities, a different `predict`
shape entirely) is a natural extension this module doesn't attempt, to
avoid a half-tested, type-inconsistent second code path.
"""

from collections.abc import Callable

import numpy as np
import numpy.typing as npt
import pandas as pd

from app.domain.exceptions.research import ResearchError
from app.research.models import ExplanationMethod, LocalExplanation

PredictFunction = Callable[[pd.DataFrame], npt.NDArray[np.float64]]

#: LIME's regression mode internally represents an explanation as a
#: pseudo-2-class problem; label `1` is the one whose signs match
#: `lime.explanation.Explanation.as_list()`'s human-readable output —
#: verified directly against the installed `lime` package, not assumed.
_LIME_REGRESSION_LABEL = 1


def explain_with_shap(
    predict: PredictFunction,
    background: pd.DataFrame,
    instances: pd.DataFrame,
    *,
    seed: int | None = None,
) -> tuple[pd.DataFrame, list[LocalExplanation]]:
    """SHAP explanations for every row of `instances`.

    Args:
        predict: The (already-fitted) model's prediction function.
        background: A representative sample used as SHAP's reference
            distribution (`shap.Explainer`'s "masker") — typically the
            training set, or a subsample of it for speed.
        instances: The rows to explain.
        seed: Reproducibility seed.

    Returns:
        `(shap_values, explanations)`: the raw per-row, per-feature SHAP
        values as a DataFrame — feed this to
        `feature_importance.compute_importance_from_shap` for a global
        ranking — and the same values wrapped as `LocalExplanation`s.

    Raises:
        ResearchError: `shap` isn't installed, or `background`/`instances`
            is empty.
    """

    try:
        import shap
    except ImportError as exc:
        raise ResearchError(
            "SHAP explanations require the optional 'shap' package "
            "(install with the 'research' extra)"
        ) from exc

    if background.empty or instances.empty:
        raise ResearchError("SHAP explanations require non-empty background and instances")

    explainer = shap.Explainer(predict, background, seed=seed)
    explanation = explainer(instances)

    shap_values = pd.DataFrame(
        explanation.values, columns=list(instances.columns), index=instances.index
    )
    base_values = np.asarray(explanation.base_values)
    predictions = predict(instances)

    explanations = [
        LocalExplanation(
            method=ExplanationMethod.SHAP,
            prediction_index=position,
            base_value=float(base_values[position]),
            feature_contributions=dict(
                zip(
                    instances.columns,
                    (float(value) for value in shap_values.iloc[position]),
                    strict=True,
                )
            ),
            predicted_value=float(predictions[position]),
        )
        for position in range(len(instances))
    ]

    return shap_values, explanations


def explain_with_lime(
    predict: PredictFunction,
    training_data: pd.DataFrame,
    instances: pd.DataFrame,
    *,
    num_features: int | None = None,
    seed: int | None = None,
) -> list[LocalExplanation]:
    """LIME explanations for every row of `instances`.

    Args:
        predict: The (already-fitted) model's prediction function.
        training_data: A representative sample LIME uses to learn each
            feature's value distribution for local perturbation.
        instances: The rows to explain.
        num_features: How many features to include per explanation;
            defaults to every column in `instances`.
        seed: Reproducibility seed.

    Raises:
        ResearchError: `lime` isn't installed, or `training_data`/
            `instances` is empty.
    """

    try:
        from lime.lime_tabular import LimeTabularExplainer
    except ImportError as exc:
        raise ResearchError(
            "LIME explanations require the optional 'lime' package "
            "(install with the 'research' extra)"
        ) from exc

    if training_data.empty or instances.empty:
        raise ResearchError("LIME explanations require non-empty training_data and instances")

    feature_names = list(instances.columns)
    resolved_num_features = num_features or len(feature_names)

    explainer = LimeTabularExplainer(
        training_data.to_numpy(), feature_names=feature_names, mode="regression", random_state=seed
    )

    explanations: list[LocalExplanation] = []
    for position in range(len(instances)):
        row = instances.iloc[position].to_numpy()
        explanation = explainer.explain_instance(row, predict, num_features=resolved_num_features)
        contributions = {
            feature_names[index]: float(weight)
            for index, weight in explanation.local_exp[_LIME_REGRESSION_LABEL]
        }

        explanations.append(
            LocalExplanation(
                method=ExplanationMethod.LIME,
                prediction_index=position,
                base_value=float(explanation.intercept[_LIME_REGRESSION_LABEL]),
                feature_contributions=contributions,
                predicted_value=float(explanation.predicted_value),
            )
        )

    return explanations
