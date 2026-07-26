"""Hyperparameter Optimization: searches a
`HyperparameterOptimizationConfig`'s parameter space for the
objective-maximizing (or -minimizing) combination.

Grid search and random search are pure standard library, no dependency
beyond it. `OptimizationMethod.BAYESIAN` delegates to Optuna's TPE
(Tree-structured Parzen Estimator) sampler — an optional 'research'
extra, imported lazily so this module (and grid/random search) works
without it installed.

The caller-supplied `ObjectiveFunction` is the one piece of "run a
trial" logic this module doesn't own: whether a trial means running a
full backtest, training an ML model, or something else entirely is the
caller's business — this module only decides *which* parameter
combinations to try and *in what order*.
"""

from __future__ import annotations

import itertools
import random
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from app.analytics.dto import OptimizationTrialResult
from app.domain.exceptions.research import HyperparameterOptimizationError
from app.research.dto import HyperparameterOptimizationConfig, OptimizationMethod, ParameterRange
from app.research.models import HyperparameterOptimizationResult

if TYPE_CHECKING:
    import optuna

#: Runs one trial: given a resolved parameter combination, returns
#: `(objective_value, total_trades)`. `total_trades` is `0` for
#: objectives with no natural "trade count" (e.g. tuning an ML model
#: against a validation metric rather than a strategy backtest).
ObjectiveFunction = Callable[[dict[str, float]], tuple[float, int]]

#: Floating-point step accumulation (e.g. repeatedly adding 0.1) can land
#: just under a boundary that should be included; this tolerance keeps
#: grid search from silently dropping the final value in a range.
_GRID_BOUNDARY_TOLERANCE = 1e-9


def optimize(
    config: HyperparameterOptimizationConfig,
    objective: ObjectiveFunction,
    *,
    seed: int | None = None,
) -> HyperparameterOptimizationResult:
    """Search `config`'s parameter space, returning every trial plus the
    best one found.

    Args:
        config: What to search and how (method, ranges, trial budget,
            objective name/direction).
        objective: Runs one trial for a given parameter combination.
        seed: Reproducibility seed for `RANDOM_SEARCH`/`BAYESIAN`.
            Ignored for `GRID_SEARCH`, which is deterministic already.

    Raises:
        HyperparameterOptimizationError: `GRID_SEARCH` was requested for
            a parameter range with no `step`, the search produced zero
            trials, or `BAYESIAN` was requested without Optuna installed.
    """

    started = time.monotonic()

    if config.method is OptimizationMethod.GRID_SEARCH:
        trials = _run_grid_search(config, objective)
    elif config.method is OptimizationMethod.RANDOM_SEARCH:
        trials = _run_random_search(config, objective, seed=seed)
    else:
        trials = _run_bayesian_search(config, objective, seed=seed)

    elapsed_seconds = time.monotonic() - started

    if not trials:
        raise HyperparameterOptimizationError("optimization produced no trials")

    best_trial = (max if config.maximize else min)(trials, key=lambda trial: trial.objective_value)

    return HyperparameterOptimizationResult(
        trials=trials, best_trial=best_trial, total_duration_seconds=elapsed_seconds
    )


def _make_trial(
    config: HyperparameterOptimizationConfig, params: dict[str, float], objective: ObjectiveFunction
) -> OptimizationTrialResult:
    objective_value, total_trades = objective(params)
    return OptimizationTrialResult(
        parameters=params,
        objective_metric_name=config.objective_metric_name,
        objective_value=objective_value,
        total_trades=total_trades,
    )


def _run_grid_search(
    config: HyperparameterOptimizationConfig, objective: ObjectiveFunction
) -> list[OptimizationTrialResult]:
    """Exhaustively try every combination of each range's grid values.

    `config.max_trials` is deliberately not applied here: grid search's
    entire purpose is exhaustive coverage of the declared grid, and
    silently truncating it to a trial budget would defeat that without
    the caller necessarily noticing.
    """

    names = [param_range.name for param_range in config.parameter_ranges]
    value_grids = [_grid_values(param_range) for param_range in config.parameter_ranges]

    return [
        _make_trial(config, dict(zip(names, combination, strict=True)), objective)
        for combination in itertools.product(*value_grids)
    ]


def _grid_values(param_range: ParameterRange) -> list[float]:
    if param_range.step is None:
        raise HyperparameterOptimizationError(
            f"grid search requires a `step` for parameter {param_range.name!r}"
        )

    values: list[float] = []
    current = param_range.low
    while current <= param_range.high + _GRID_BOUNDARY_TOLERANCE:
        values.append(float(round(current)) if param_range.is_integer else current)
        current += param_range.step
    return values


def _run_random_search(
    config: HyperparameterOptimizationConfig, objective: ObjectiveFunction, *, seed: int | None
) -> list[OptimizationTrialResult]:
    rng = random.Random(seed)
    return [
        _make_trial(
            config,
            {
                param_range.name: _sample_uniform(rng, param_range)
                for param_range in config.parameter_ranges
            },
            objective,
        )
        for _ in range(config.max_trials)
    ]


def _sample_uniform(rng: random.Random, param_range: ParameterRange) -> float:
    if param_range.is_integer:
        return float(rng.randint(int(param_range.low), int(param_range.high)))
    return rng.uniform(param_range.low, param_range.high)


def _run_bayesian_search(
    config: HyperparameterOptimizationConfig, objective: ObjectiveFunction, *, seed: int | None
) -> list[OptimizationTrialResult]:
    try:
        import optuna
    except ImportError as exc:
        raise HyperparameterOptimizationError(
            "Bayesian optimization requires the optional 'optuna' package "
            "(install with the 'research' extra) or use GRID_SEARCH/RANDOM_SEARCH instead"
        ) from exc

    # Optuna logs one line per trial at its default verbosity, which
    # would otherwise flood output for even a modest trial budget.
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    direction = "maximize" if config.maximize else "minimize"
    study = optuna.create_study(direction=direction, sampler=optuna.samplers.TPESampler(seed=seed))

    trials: list[OptimizationTrialResult] = []

    def _optuna_objective(trial: optuna.Trial) -> float:
        params = {
            param_range.name: _suggest(trial, param_range)
            for param_range in config.parameter_ranges
        }
        result = _make_trial(config, params, objective)
        trials.append(result)
        return result.objective_value

    study.optimize(_optuna_objective, n_trials=config.max_trials)
    return trials


def _suggest(trial: optuna.Trial, param_range: ParameterRange) -> float:
    if param_range.is_integer:
        step = int(param_range.step) if param_range.step is not None else 1
        return float(
            trial.suggest_int(
                param_range.name, int(param_range.low), int(param_range.high), step=step
            )
        )
    return trial.suggest_float(
        param_range.name, param_range.low, param_range.high, step=param_range.step
    )
