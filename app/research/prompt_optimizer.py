"""Prompt Optimization: evaluates `PromptVariant`s against each other for
offline research — never live serving.

Mirrors `app.research.hyperparameter_optimizer`'s shape: the caller
supplies an evaluation function (this module has no LLM client and never
calls one — running a prompt against a model is `app.ai`/`app.ai_agents`'s
job), and this module only decides which variant is best and by how much.
"""

from collections.abc import Callable, Sequence

from app.domain.exceptions.research import ResearchError
from app.research.dto import PromptVariant
from app.research.models import PromptEvaluationScore, PromptOptimizationResult

#: Runs one variant against a test set and returns its aggregate
#: measured performance: `(accuracy_percent, average_confidence,
#: average_latency_ms, average_cost_usd, sample_count)`.
EvaluationFunction = Callable[[PromptVariant], tuple[float, float, float, float, int]]

#: Which `PromptEvaluationScore` fields `optimize_prompts` can select a
#: winner on.
_OBJECTIVE_FIELDS = frozenset(
    {"accuracy_percent", "average_confidence", "average_latency_ms", "average_cost_usd"}
)


def optimize_prompts(
    variants: Sequence[PromptVariant],
    evaluate: EvaluationFunction,
    *,
    objective_metric: str = "accuracy_percent",
    maximize: bool = True,
) -> PromptOptimizationResult:
    """Evaluate every `PromptVariant` in `variants`, returning each one's
    measured performance plus which one is best.

    Args:
        variants: Candidate prompt versions to compare.
        evaluate: Runs one variant against a test set.
        objective_metric: Which `PromptEvaluationScore` field selects the
            winner.
        maximize: Whether a higher `objective_metric` value is better —
            `False` for `average_latency_ms`/`average_cost_usd`, where
            lower is better.

    Raises:
        ResearchError: `variants` is empty, or `objective_metric` isn't
            a recognized `PromptEvaluationScore` field.
    """

    if not variants:
        raise ResearchError("cannot optimize prompts with zero variants")

    if objective_metric not in _OBJECTIVE_FIELDS:
        raise ResearchError(
            f"unknown objective_metric {objective_metric!r}; expected one of "
            f"{sorted(_OBJECTIVE_FIELDS)}"
        )

    scores = [_evaluate_variant(variant, evaluate) for variant in variants]
    best = (max if maximize else min)(scores, key=lambda score: getattr(score, objective_metric))

    return PromptOptimizationResult(
        scores=scores, best_version=best.version, objective_metric=objective_metric
    )


def _evaluate_variant(
    variant: PromptVariant, evaluate: EvaluationFunction
) -> PromptEvaluationScore:
    accuracy_percent, average_confidence, average_latency_ms, average_cost_usd, sample_count = (
        evaluate(variant)
    )
    return PromptEvaluationScore(
        version=variant.version,
        accuracy_percent=accuracy_percent,
        average_confidence=average_confidence,
        average_latency_ms=average_latency_ms,
        average_cost_usd=average_cost_usd,
        sample_count=sample_count,
    )
