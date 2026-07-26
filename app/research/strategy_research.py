"""Strategy Research: the top-level facade tying dataset creation,
experiment tracking, and hyperparameter optimization into one "research
a strategy" workflow.

Walk-forward analysis, Monte Carlo robustness, and statistical analysis
(correlation/cointegration/PCA/clustering/regime) remain separate,
directly-callable utilities (`walk_forward.py`, `monte_carlo.py`,
`statistical_analysis.py`, `regime_analysis.py`) rather than folded into
this facade — they're independently useful steps a researcher may or may
not want for a given session, not a fixed sequence every strategy-research
call needs.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pandas as pd

from app.indicators.schemas import IndicatorResult
from app.market.dto import MarketCandle
from app.research.dataset_manager import DatasetManager
from app.research.dto import DatasetRequest, ExperimentConfig, HyperparameterOptimizationConfig
from app.research.experiment_tracker import ExperimentTracker
from app.research.hyperparameter_optimizer import ObjectiveFunction, optimize
from app.research.models import StrategyResearchResult

#: Given the built feature matrix, returns the `ObjectiveFunction`
#: `hyperparameter_optimizer.optimize` should search over — the caller's
#: job is knowing how to turn a parameter combination into a
#: backtest/score over this specific matrix.
ObjectiveFactory = Callable[[pd.DataFrame], ObjectiveFunction]


class StrategyResearchSession:
    """One research session: builds a dataset once, then can run one or
    more hyperparameter searches against it, each tracked as its own
    experiment run.

    Holds a `DatasetManager` and an `ExperimentTracker` — inject one
    long-lived instance of each (matching every other engine in this
    project's dependency-injection convention) to actually benefit from
    `DatasetManager`'s caching across calls.
    """

    def __init__(
        self, dataset_manager: DatasetManager, experiment_tracker: ExperimentTracker
    ) -> None:
        self._dataset_manager = dataset_manager
        self._experiment_tracker = experiment_tracker

    def research_strategy(
        self,
        dataset_request: DatasetRequest,
        candles: Sequence[MarketCandle],
        indicators: Mapping[str, IndicatorResult[Any]] | None,
        experiment_config: ExperimentConfig,
        optimization_config: HyperparameterOptimizationConfig,
        make_objective: ObjectiveFactory,
        *,
        seed: int | None = None,
    ) -> StrategyResearchResult:
        """Build the dataset once, then search `optimization_config`'s
        parameter space against it, tracking the whole run as one experiment.

        The experiment run is always ended — even if building the
        objective or the search itself raises — so a failed run is
        still recorded as failed, not left open indefinitely; the
        original exception still propagates to the caller afterward.

        Args:
            dataset_request: What feature matrix to build.
            candles: Candle history `dataset_request` is built from.
            indicators: Already-computed indicators `dataset_request`'s
                `FeatureSpec`s may reference.
            experiment_config: Identity/tracking backend for this run.
            optimization_config: Search configuration for the strategy's
                parameters.
            make_objective: Given the built feature matrix, returns the
                objective function to search.
            seed: Reproducibility seed for the parameter search.
        """

        dataset, dataset_summary = self._dataset_manager.build(dataset_request, candles, indicators)

        run_id = self._experiment_tracker.start_run(experiment_config)
        try:
            objective = make_objective(dataset)
            optimization_result = optimize(optimization_config, objective, seed=seed)

            self._experiment_tracker.log_params(run_id, optimization_result.best_trial.parameters)
            self._experiment_tracker.log_metrics(
                run_id,
                {
                    optimization_config.objective_metric_name: (
                        optimization_result.best_trial.objective_value
                    ),
                    "total_duration_seconds": optimization_result.total_duration_seconds,
                },
            )
        finally:
            experiment_record = self._experiment_tracker.end_run(run_id)

        return StrategyResearchResult(
            dataset_summary=dataset_summary,
            optimization_result=optimization_result,
            experiment_record=experiment_record,
        )
