"""Quant Research Platform output models.

Reuses `app.analytics.dto.OptimizationTrialResult` for one optimization
trial's result — the same "parameters in, one objective metric out"
shape whether the trial came from a Portfolio Analytics optimization
report or an Optuna study here, so it isn't redefined.

Large numeric artifacts (a built feature matrix, a trained model) are
never modeled as Pydantic fields — they stay pandas DataFrames / model
objects. Everything here describes *metadata about* or *the result of*
an analysis, which is what every consumer (a report, an experiment log,
a comparison) actually needs.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.analytics.dto import OptimizationTrialResult
from app.domain.enums.trading import Exchange, HistoricalInterval

# --------------------------------------------------------------------------
# Dataset
# --------------------------------------------------------------------------


class DatasetSummary(BaseModel):
    """Metadata about a feature matrix `dataset_manager.py` built."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    exchange: Exchange
    interval: HistoricalInterval
    from_date: datetime
    to_date: datetime
    row_count: int = Field(ge=0)
    feature_names: list[str] = Field(min_length=1)
    label_name: str = Field(min_length=1)
    missing_value_count: int = Field(ge=0)
    content_hash: str = Field(min_length=1)


# --------------------------------------------------------------------------
# Feature importance / model comparison
# --------------------------------------------------------------------------


class FeatureImportanceScore(BaseModel):
    """One feature's importance within a `FeatureImportanceResult`."""

    model_config = ConfigDict(frozen=True)

    feature_name: str = Field(min_length=1)
    importance: float
    rank: int = Field(ge=1)


class FeatureImportanceResult(BaseModel):
    """`feature_importance.py`'s output: every feature's importance
    score under one computation method (e.g. permutation importance,
    gain-based, or mean absolute SHAP value).
    """

    model_config = ConfigDict(frozen=True)

    method: str = Field(min_length=1)
    scores: list[FeatureImportanceScore] = Field(min_length=1)
    computed_at: datetime


class ModelPerformanceSummary(BaseModel):
    """One candidate model's performance, within a `ModelComparisonResult`."""

    model_config = ConfigDict(frozen=True)

    model_name: str = Field(min_length=1)
    metric_name: str = Field(min_length=1)
    metric_value: float
    training_duration_seconds: float = Field(ge=0)


class ModelComparisonResult(BaseModel):
    """`model_comparison.py`'s output: every candidate model's
    performance on the same metric, and which one won.
    """

    model_config = ConfigDict(frozen=True)

    candidates: list[ModelPerformanceSummary] = Field(min_length=1)
    best_model_name: str = Field(min_length=1)
    comparison_metric: str = Field(min_length=1)


# --------------------------------------------------------------------------
# Explainability (SHAP / LIME)
# --------------------------------------------------------------------------


class ExplanationMethod(StrEnum):
    """Which explainability technique produced a `LocalExplanation`."""

    SHAP = "SHAP"
    LIME = "LIME"


class LocalExplanation(BaseModel):
    """One prediction's per-feature attribution — `explainability.py`'s
    output for a single row of a dataset, not a whole model's behavior
    (see `FeatureImportanceResult` for the global view).
    """

    model_config = ConfigDict(frozen=True)

    method: ExplanationMethod
    prediction_index: int = Field(ge=0)
    base_value: float
    feature_contributions: dict[str, float] = Field(min_length=1)
    predicted_value: float


# --------------------------------------------------------------------------
# Statistics: correlation / cointegration / PCA / clustering
# --------------------------------------------------------------------------


class CorrelationResult(BaseModel):
    """A symbol-by-symbol correlation matrix from `statistical_analysis.py`."""

    model_config = ConfigDict(frozen=True)

    symbols: list[str] = Field(min_length=2)
    matrix: list[list[float]]
    method: str = "pearson"

    @model_validator(mode="after")
    def _check_matrix_is_square(self) -> "CorrelationResult":
        n = len(self.symbols)
        if len(self.matrix) != n or any(len(row) != n for row in self.matrix):
            raise ValueError(f"matrix must be {n}x{n} to match `symbols`")
        return self


class CointegrationResult(BaseModel):
    """An Engle-Granger (or similar) cointegration test result for one
    pair of symbols, from `statistical_analysis.py`.
    """

    model_config = ConfigDict(frozen=True)

    symbol_a: str = Field(min_length=1)
    symbol_b: str = Field(min_length=1)
    test_statistic: float
    p_value: float = Field(ge=0, le=1)
    critical_values: dict[str, float] = Field(min_length=1)
    is_cointegrated: bool
    hedge_ratio: float | None = None


class PCAComponent(BaseModel):
    """One principal component from a `PCAResult`."""

    model_config = ConfigDict(frozen=True)

    component_index: int = Field(ge=0)
    explained_variance_ratio: float = Field(ge=0, le=1)
    loadings: dict[str, float] = Field(min_length=1)


class PCAResult(BaseModel):
    """`statistical_analysis.py`'s PCA output: every retained component
    plus the cumulative variance they explain together.
    """

    model_config = ConfigDict(frozen=True)

    components: list[PCAComponent] = Field(min_length=1)
    cumulative_explained_variance: float = Field(ge=0, le=1)


class ClusterAssignment(BaseModel):
    """One symbol's assigned cluster, within a `ClusteringResult`."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    cluster_id: int = Field(ge=0)


class ClusteringResult(BaseModel):
    """`statistical_analysis.py`'s clustering output (e.g. k-means or
    hierarchical clustering over symbols' return/feature profiles).
    """

    model_config = ConfigDict(frozen=True)

    method: str = Field(min_length=1)
    assignments: list[ClusterAssignment] = Field(min_length=1)
    cluster_count: int = Field(gt=0)
    silhouette_score: float | None = Field(default=None, ge=-1.0, le=1.0)

    @model_validator(mode="after")
    def _check_cluster_ids_within_range(self) -> "ClusteringResult":
        max_id = max(assignment.cluster_id for assignment in self.assignments)
        if max_id >= self.cluster_count:
            raise ValueError(
                f"cluster_id {max_id} is out of range for cluster_count={self.cluster_count}"
            )
        return self


# --------------------------------------------------------------------------
# Regime analysis
# --------------------------------------------------------------------------


class MarketRegimeLabel(StrEnum):
    """A statistically-classified market regime, from `regime_analysis.py`.

    Distinct from whatever `app.ai_agents.agents.market_regime_agent`
    eventually returns once it's built: this is a deterministic
    classification (rule-based or statistical/clustering-based) used for
    research, not an LLM's judgment used for live decision support — the
    two may disagree, which is itself useful signal, not a bug.
    """

    TRENDING = "TRENDING"
    RANGE = "RANGE"
    VOLATILE = "VOLATILE"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    BREAKOUT = "BREAKOUT"
    REVERSAL = "REVERSAL"


class RegimePeriod(BaseModel):
    """One contiguous span classified as a single `MarketRegimeLabel`."""

    model_config = ConfigDict(frozen=True)

    regime: MarketRegimeLabel
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _check_period_ordering(self) -> "RegimePeriod":
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class RegimeAnalysisResult(BaseModel):
    """`regime_analysis.py`'s output: the full regime timeline for one
    symbol, plus how much of the analyzed history each regime occupied.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    periods: list[RegimePeriod] = Field(min_length=1)
    regime_frequency_percent: dict[MarketRegimeLabel, float] = Field(min_length=1)


# --------------------------------------------------------------------------
# Monte Carlo
# --------------------------------------------------------------------------


class MonteCarloResult(BaseModel):
    """`monte_carlo.py`'s output: robustness estimates from randomized
    resampling of a trade/return history.
    """

    model_config = ConfigDict(frozen=True)

    simulation_count: int = Field(gt=0)
    expected_drawdown_percent: float = Field(ge=0)
    drawdown_confidence_intervals: dict[str, float] = Field(min_length=1)
    return_percentiles: dict[str, float] = Field(min_length=1)
    risk_of_ruin_percent: float = Field(ge=0, le=100)


# --------------------------------------------------------------------------
# Walk-forward analysis
# --------------------------------------------------------------------------


class WalkForwardWindowResult(BaseModel):
    """One train/validate window's result within a `WalkForwardReport`."""

    model_config = ConfigDict(frozen=True)

    window_index: int = Field(ge=0)
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime
    best_parameters: dict[str, float] = Field(min_length=1)
    train_objective_value: float
    validation_objective_value: float

    @model_validator(mode="after")
    def _check_window_ordering(self) -> "WalkForwardWindowResult":
        if not (self.train_start < self.train_end <= self.validation_start < self.validation_end):
            raise ValueError(
                "window boundaries must satisfy "
                "train_start < train_end <= validation_start < validation_end"
            )
        return self


class WalkForwardReport(BaseModel):
    """`walk_forward.py`'s output: every rolled-forward window plus the
    aggregate train-vs-validation performance gap (a large gap signals
    overfitting to the training window).
    """

    model_config = ConfigDict(frozen=True)

    windows: list[WalkForwardWindowResult] = Field(min_length=1)
    average_validation_objective: float
    objective_degradation_percent: float


# --------------------------------------------------------------------------
# Hyperparameter optimization
# --------------------------------------------------------------------------


class HyperparameterOptimizationResult(BaseModel):
    """`hyperparameter_optimizer.py`'s output: every trial run plus
    whichever one scored best on the configured objective.
    """

    model_config = ConfigDict(frozen=True)

    trials: list[OptimizationTrialResult] = Field(min_length=1)
    best_trial: OptimizationTrialResult
    total_duration_seconds: float = Field(ge=0)

    @model_validator(mode="after")
    def _check_best_trial_is_one_of_trials(self) -> "HyperparameterOptimizationResult":
        if self.best_trial not in self.trials:
            raise ValueError("best_trial must be one of trials")
        return self


# --------------------------------------------------------------------------
# Prompt optimization
# --------------------------------------------------------------------------


class PromptEvaluationScore(BaseModel):
    """One `PromptVariant`'s measured performance, within a
    `PromptOptimizationResult`.
    """

    model_config = ConfigDict(frozen=True)

    version: str = Field(min_length=1)
    accuracy_percent: float = Field(ge=0, le=100)
    average_confidence: float = Field(ge=0, le=100)
    average_latency_ms: float = Field(ge=0)
    average_cost_usd: float = Field(ge=0)
    sample_count: int = Field(gt=0)


class PromptOptimizationResult(BaseModel):
    """`prompt_optimizer.py`'s output: every candidate `PromptVariant`'s
    measured performance plus which one won on the configured objective.
    """

    model_config = ConfigDict(frozen=True)

    scores: list[PromptEvaluationScore] = Field(min_length=1)
    best_version: str = Field(min_length=1)
    objective_metric: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_best_version_is_scored(self) -> "PromptOptimizationResult":
        if self.best_version not in {score.version for score in self.scores}:
            raise ValueError("best_version must be one of scores' versions")
        return self


# --------------------------------------------------------------------------
# Experiment tracking
# --------------------------------------------------------------------------


class ExperimentRecord(BaseModel):
    """One completed research experiment, as recorded by
    `experiment_tracker.py` (in MLflow or in-memory, per
    `app.research.dto.ExperimentConfig.tracking_backend`).
    """

    model_config = ConfigDict(frozen=True)

    experiment_name: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)
    tags: dict[str, str] = Field(default_factory=dict)
    started_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def _check_completed_after_started(self) -> "ExperimentRecord":
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at cannot be before started_at")
        return self


# --------------------------------------------------------------------------
# Strategy research
# --------------------------------------------------------------------------


class StrategyResearchResult(BaseModel):
    """`strategy_research.py`'s output: one research session's dataset,
    the parameter search performed against it, and the tracked
    experiment record for the whole run.
    """

    model_config = ConfigDict(frozen=True)

    dataset_summary: DatasetSummary
    optimization_result: HyperparameterOptimizationResult
    experiment_record: ExperimentRecord


# --------------------------------------------------------------------------
# Research report
# --------------------------------------------------------------------------


class ResearchReport(BaseModel):
    """The top-level artifact `report_generator.py` produces, tying
    together whichever of this module's analyses were run for one
    research session. Every section is optional: a report from a
    correlation-only session has no feature importance to show, and
    vice versa.
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1)
    generated_at: datetime
    dataset_summary: DatasetSummary | None = None
    feature_importance: FeatureImportanceResult | None = None
    model_comparison: ModelComparisonResult | None = None
    correlation: CorrelationResult | None = None
    cointegration: list[CointegrationResult] = Field(default_factory=list)
    pca: PCAResult | None = None
    clustering: ClusteringResult | None = None
    regime_analysis: RegimeAnalysisResult | None = None
    monte_carlo: MonteCarloResult | None = None
    walk_forward: WalkForwardReport | None = None
    optimization: HyperparameterOptimizationResult | None = None
    notes: list[str] = Field(default_factory=list)
