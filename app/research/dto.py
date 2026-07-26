"""Quant Research Platform input DTOs.

The Research Platform never interacts with live trading — every
capability here (feature engineering, dataset creation, optimization,
statistical analysis) operates on historical data and produces research
artifacts (datasets, experiment records, statistical results), never an
order or a broker call.

Reuses `Exchange`/`HistoricalInterval` from `app.domain.enums.trading`
rather than re-declaring them.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums.trading import Exchange, HistoricalInterval


class FeatureType(StrEnum):
    """How `feature_engineering.py` derives one engineered feature."""

    INDICATOR = "INDICATOR"
    PRICE_DERIVED = "PRICE_DERIVED"
    VOLUME_DERIVED = "VOLUME_DERIVED"
    LAGGED = "LAGGED"
    ROLLING_STATISTIC = "ROLLING_STATISTIC"


class RollingStatistic(StrEnum):
    """Which rolling-window statistic a `ROLLING_STATISTIC` feature computes."""

    MEAN = "MEAN"
    STD = "STD"
    MIN = "MIN"
    MAX = "MAX"
    SKEW = "SKEW"
    KURTOSIS = "KURTOSIS"
    ZSCORE = "ZSCORE"


class FeatureSpec(BaseModel):
    """One engineered feature's definition.

    `source_indicator`/`source_field` name where the raw value comes
    from — an indicator name plus its point-model field (e.g. `"RSI"` /
    `"value"`), or a raw OHLCV column (e.g. `"close"` / `"close"`).
    `feature_type` says what transform, if any, `feature_engineering.py`
    applies on top of that raw source.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    feature_type: FeatureType
    source_indicator: str | None = None
    source_field: str = Field(default="value", min_length=1)
    lag_periods: int | None = Field(default=None, gt=0)
    rolling_window: int | None = Field(default=None, gt=0)
    rolling_statistic: RollingStatistic | None = None

    @model_validator(mode="after")
    def _check_transform_params(self) -> "FeatureSpec":
        """Each transform type needs exactly the parameters that give it
        meaning — a LAGGED feature with no lag, or a ROLLING_STATISTIC
        with no window/statistic, can't actually be computed.
        """

        if self.feature_type is FeatureType.LAGGED and self.lag_periods is None:
            raise ValueError("LAGGED feature requires lag_periods")

        if self.feature_type is FeatureType.ROLLING_STATISTIC and (
            self.rolling_window is None or self.rolling_statistic is None
        ):
            raise ValueError(
                "ROLLING_STATISTIC feature requires rolling_window and rolling_statistic"
            )

        return self


class DatasetRequest(BaseModel):
    """A request to `dataset_manager.py` to assemble one ML-ready feature
    matrix for a symbol over a date range.

    `label_horizon_bars` is how many bars ahead `dataset_manager.py`
    looks to compute a supervised-learning label (e.g. forward return);
    it's a dataset-construction parameter, not a feature, so it lives
    here rather than in `features`.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    exchange: Exchange
    interval: HistoricalInterval
    from_date: datetime
    to_date: datetime
    features: list[FeatureSpec] = Field(min_length=1)
    label_horizon_bars: int = Field(default=1, gt=0)

    @model_validator(mode="after")
    def _check_date_range(self) -> "DatasetRequest":
        if self.from_date >= self.to_date:
            raise ValueError("from_date must be before to_date")
        return self


class ExperimentTrackingBackend(StrEnum):
    """Where `experiment_tracker.py` records a research run."""

    MLFLOW = "MLFLOW"
    IN_MEMORY = "IN_MEMORY"


class ExperimentConfig(BaseModel):
    """One research experiment's identity and tracking configuration."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    tags: dict[str, str] = Field(default_factory=dict)
    tracking_backend: ExperimentTrackingBackend = ExperimentTrackingBackend.IN_MEMORY
    tracking_uri: str | None = None


class OptimizationMethod(StrEnum):
    """Search strategy `hyperparameter_optimizer.py` uses.

    `BAYESIAN` is Optuna's TPE (Tree-structured Parzen Estimator)
    sampler — chosen over grid/random search once enough trials have run
    to model which regions of the search space look promising.
    """

    GRID_SEARCH = "GRID_SEARCH"
    RANDOM_SEARCH = "RANDOM_SEARCH"
    BAYESIAN = "BAYESIAN"


class ParameterRange(BaseModel):
    """One hyperparameter's search space."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    low: float
    high: float
    step: float | None = Field(default=None, gt=0)
    is_integer: bool = False

    @model_validator(mode="after")
    def _check_bounds(self) -> "ParameterRange":
        if self.low >= self.high:
            raise ValueError("low must be less than high")
        return self


class HyperparameterOptimizationConfig(BaseModel):
    """Configuration for one hyperparameter optimization run."""

    model_config = ConfigDict(frozen=True)

    method: OptimizationMethod = OptimizationMethod.BAYESIAN
    parameter_ranges: list[ParameterRange] = Field(min_length=1)
    objective_metric_name: str = Field(default="sharpe_ratio", min_length=1)
    maximize: bool = True
    max_trials: int = Field(gt=0)


class PromptVariant(BaseModel):
    """One candidate prompt version for `prompt_optimizer.py` to evaluate
    against the others.

    Mirrors the prompt-versioning concept `app.ai_agents` is designed
    around (version/author/date/description), but scoped to offline
    research/comparison — this module never serves a prompt to a live
    agent, it only measures which version performs better.
    """

    model_config = ConfigDict(frozen=True)

    version: str = Field(min_length=1)
    template: str = Field(min_length=1)
    author: str = Field(min_length=1)
    description: str = Field(min_length=1)
    created_at: datetime


class WalkForwardConfig(BaseModel):
    """Window sizing for `walk_forward.py`'s rolling train/validate split."""

    model_config = ConfigDict(frozen=True)

    train_period_days: int = Field(gt=0)
    validation_period_days: int = Field(gt=0)
    step_days: int = Field(gt=0)
