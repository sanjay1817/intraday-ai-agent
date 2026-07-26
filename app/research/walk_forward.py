"""Walk Forward Analysis: rolls a train/validate window forward across a
historical range, re-optimizing on each training window and grading the
result out-of-sample on the following validation window.

Reuses `app.research.hyperparameter_optimizer.optimize` for each
window's training step — walk-forward analysis is fundamentally
"optimize, then validate out-of-sample, repeatedly," not a different
optimization mechanism, so it doesn't reimplement one.
"""

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta

from app.domain.exceptions.research import ResearchError
from app.research.dto import HyperparameterOptimizationConfig, WalkForwardConfig
from app.research.hyperparameter_optimizer import ObjectiveFunction, optimize
from app.research.models import WalkForwardReport, WalkForwardWindowResult

#: Given a training window's (start, end), returns the `ObjectiveFunction`
#: `hyperparameter_optimizer.optimize` should search over — the caller's
#: job is knowing how to slice its own dataset/backtest to that period.
ObjectiveFactory = Callable[[datetime, datetime], ObjectiveFunction]

#: Given a fixed parameter set and a validation window's (start, end),
#: returns the objective value achieved on that period with those
#: parameters — no search, just an out-of-sample evaluation.
ValidationFunction = Callable[[dict[str, float], datetime, datetime], float]


def run_walk_forward(
    optimization_config: HyperparameterOptimizationConfig,
    walk_forward_config: WalkForwardConfig,
    overall_start: datetime,
    overall_end: datetime,
    make_training_objective: ObjectiveFactory,
    evaluate_validation: ValidationFunction,
    *,
    seed: int | None = None,
) -> WalkForwardReport:
    """Roll a train/validate window across `[overall_start, overall_end)`,
    re-running `optimize` on each training window and grading its best
    parameters out-of-sample on the following validation window.

    Args:
        optimization_config: Search configuration reused for every
            window's training step.
        walk_forward_config: Train/validation/step window sizes, in days.
        overall_start: Start of the full historical range to walk over.
        overall_end: End of the full historical range (exclusive — the
            last window's validation period ends at or before this).
        make_training_objective: Given one window's train period, builds
            the `ObjectiveFunction` to optimize against just that slice
            of data.
        evaluate_validation: Given the winning parameters and one
            window's validation period, returns the out-of-sample
            objective value achieved with those parameters.
        seed: Reproducibility seed, forwarded to `optimize` for every window.

    Raises:
        ResearchError: no window fits within
            `[overall_start, overall_end)` given the configured window sizes.
    """

    windows = list(_generate_windows(walk_forward_config, overall_start, overall_end))
    if not windows:
        raise ResearchError(
            "no window fits: check train_period_days/validation_period_days/step_days "
            "against the overall_start/overall_end range"
        )

    results = [
        _run_window(
            window_index,
            train_start,
            train_end,
            validation_start,
            validation_end,
            optimization_config,
            make_training_objective,
            evaluate_validation,
            seed,
        )
        for window_index, (train_start, train_end, validation_start, validation_end) in enumerate(
            windows
        )
    ]

    average_train = sum(result.train_objective_value for result in results) / len(results)
    average_validation = sum(result.validation_objective_value for result in results) / len(results)

    return WalkForwardReport(
        windows=results,
        average_validation_objective=average_validation,
        objective_degradation_percent=_compute_degradation(average_train, average_validation),
    )


def _run_window(
    window_index: int,
    train_start: datetime,
    train_end: datetime,
    validation_start: datetime,
    validation_end: datetime,
    optimization_config: HyperparameterOptimizationConfig,
    make_training_objective: ObjectiveFactory,
    evaluate_validation: ValidationFunction,
    seed: int | None,
) -> WalkForwardWindowResult:
    training_objective = make_training_objective(train_start, train_end)
    optimization_result = optimize(optimization_config, training_objective, seed=seed)
    best_parameters = optimization_result.best_trial.parameters

    validation_value = evaluate_validation(best_parameters, validation_start, validation_end)

    return WalkForwardWindowResult(
        window_index=window_index,
        train_start=train_start,
        train_end=train_end,
        validation_start=validation_start,
        validation_end=validation_end,
        best_parameters=best_parameters,
        train_objective_value=optimization_result.best_trial.objective_value,
        validation_objective_value=validation_value,
    )


def _generate_windows(
    config: WalkForwardConfig, overall_start: datetime, overall_end: datetime
) -> Iterator[tuple[datetime, datetime, datetime, datetime]]:
    """Yield `(train_start, train_end, validation_start, validation_end)`
    tuples, rolling forward by `config.step_days` each time, stopping
    once a validation window would extend past `overall_end`.
    """

    train_period = timedelta(days=config.train_period_days)
    validation_period = timedelta(days=config.validation_period_days)
    step = timedelta(days=config.step_days)

    train_start = overall_start
    while True:
        train_end = train_start + train_period
        validation_start = train_end
        validation_end = validation_start + validation_period
        if validation_end > overall_end:
            return
        yield train_start, train_end, validation_start, validation_end
        train_start += step


def _compute_degradation(average_train: float, average_validation: float) -> float:
    """How much worse (as a percentage of training performance)
    validation performed than training — a large positive value signals
    overfitting to the training window.

    Returns `0.0` if `average_train` is `0`: a ratio against zero is
    undefined, and reporting a fabricated number would be worse than
    reporting "no degradation to measure."
    """

    if average_train == 0:
        return 0.0
    return 100.0 * (average_train - average_validation) / abs(average_train)
