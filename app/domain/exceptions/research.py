"""Quant Research Platform exceptions."""


class ResearchError(Exception):
    """Base class for every Quant Research Platform failure."""


class FeatureEngineeringError(ResearchError):
    """Raised when a `FeatureSpec` cannot actually be computed."""


class MissingFeatureSourceError(FeatureEngineeringError):
    """Raised when a `FeatureSpec` references an indicator (or indicator
    field) that isn't present in the data supplied to
    `app.research.feature_engineering.build_feature_matrix`.
    """

    def __init__(self, feature_name: str, requested: str, available: list[str]) -> None:
        self.feature_name = feature_name
        self.requested = requested
        self.available = available
        super().__init__(
            f"feature {feature_name!r} references {requested!r}, which is not available "
            f"(available: {', '.join(available) or 'none'})"
        )


class ExperimentTrackingError(ResearchError):
    """Raised for `app.research.experiment_tracker` failures."""


class UnknownRunError(ExperimentTrackingError):
    """Raised when a tracker method references a run ID that was never
    started, or that has already ended.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"no active run {run_id!r}")


class HyperparameterOptimizationError(ResearchError):
    """Raised when a `HyperparameterOptimizationConfig` can't actually be
    searched — e.g. grid search over a parameter with no `step`, or a
    search that produced zero trials.
    """
