"""Research Reports: assembles whichever of this package's analyses were
run for one research session into a `ResearchReport`, and renders one as
human-readable Markdown.

`ResearchReport` itself (in `models.py`) already models every section as
optional — a report from a correlation-only session simply has no
feature importance to show. This module's job is twofold: (1)
`generate_report` is a thin constructor that stamps `generated_at` so
callers never have to (every other timestamped model in this package is
stamped by the module that produces it, not the caller), and (2)
`render_markdown` turns the structured report into a document a
researcher actually reads — rendering only the sections that are
present, in the same fixed order as `ResearchReport`'s fields.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from app.research.models import (
    ClusteringResult,
    CointegrationResult,
    CorrelationResult,
    DatasetSummary,
    FeatureImportanceResult,
    HyperparameterOptimizationResult,
    ModelComparisonResult,
    MonteCarloResult,
    PCAResult,
    RegimeAnalysisResult,
    ResearchReport,
    WalkForwardReport,
)


def generate_report(
    title: str,
    *,
    dataset_summary: DatasetSummary | None = None,
    feature_importance: FeatureImportanceResult | None = None,
    model_comparison: ModelComparisonResult | None = None,
    correlation: CorrelationResult | None = None,
    cointegration: Sequence[CointegrationResult] = (),
    pca: PCAResult | None = None,
    clustering: ClusteringResult | None = None,
    regime_analysis: RegimeAnalysisResult | None = None,
    monte_carlo: MonteCarloResult | None = None,
    walk_forward: WalkForwardReport | None = None,
    optimization: HyperparameterOptimizationResult | None = None,
    notes: Sequence[str] = (),
) -> ResearchReport:
    """Assemble whichever analysis results a research session produced
    into one `ResearchReport`, stamped with the current time.

    Every analysis argument is optional and independent — pass only the
    ones a given session actually ran.
    """

    return ResearchReport(
        title=title,
        generated_at=datetime.now(UTC),
        dataset_summary=dataset_summary,
        feature_importance=feature_importance,
        model_comparison=model_comparison,
        correlation=correlation,
        cointegration=list(cointegration),
        pca=pca,
        clustering=clustering,
        regime_analysis=regime_analysis,
        monte_carlo=monte_carlo,
        walk_forward=walk_forward,
        optimization=optimization,
        notes=list(notes),
    )


def render_markdown(report: ResearchReport) -> str:
    """Render `report` as a human-readable Markdown document, with only
    the sections that are actually present.
    """

    sections = [
        f"# {report.title}",
        f"_Generated {report.generated_at.isoformat()}_",
    ]

    if report.dataset_summary is not None:
        sections.append(_render_dataset_summary(report.dataset_summary))
    if report.feature_importance is not None:
        sections.append(_render_feature_importance(report.feature_importance))
    if report.model_comparison is not None:
        sections.append(_render_model_comparison(report.model_comparison))
    if report.correlation is not None:
        sections.append(_render_correlation(report.correlation))
    if report.cointegration:
        sections.append(_render_cointegration(report.cointegration))
    if report.pca is not None:
        sections.append(_render_pca(report.pca))
    if report.clustering is not None:
        sections.append(_render_clustering(report.clustering))
    if report.regime_analysis is not None:
        sections.append(_render_regime_analysis(report.regime_analysis))
    if report.monte_carlo is not None:
        sections.append(_render_monte_carlo(report.monte_carlo))
    if report.walk_forward is not None:
        sections.append(_render_walk_forward(report.walk_forward))
    if report.optimization is not None:
        sections.append(_render_optimization(report.optimization))
    if report.notes:
        sections.append("## Notes\n\n" + "\n".join(f"- {note}" for note in report.notes))

    return "\n\n".join(sections)


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    header_row = "| " + " | ".join(headers) + " |"
    separator_row = "| " + " | ".join("---" for _ in headers) + " |"
    body_rows = ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join([header_row, separator_row, *body_rows])


def _render_dataset_summary(summary: DatasetSummary) -> str:
    rows = [
        ["Symbol", f"{summary.symbol} ({summary.exchange})"],
        ["Interval", str(summary.interval)],
        ["Date range", f"{summary.from_date.isoformat()} to {summary.to_date.isoformat()}"],
        ["Rows", str(summary.row_count)],
        ["Features", ", ".join(summary.feature_names)],
        ["Label", summary.label_name],
        ["Missing values", str(summary.missing_value_count)],
        ["Content hash", summary.content_hash],
    ]
    return "## Dataset\n\n" + _markdown_table(["Field", "Value"], rows)


def _render_feature_importance(result: FeatureImportanceResult) -> str:
    ranked = sorted(result.scores, key=lambda score: score.rank)
    rows = [[str(score.rank), score.feature_name, f"{score.importance:.6g}"] for score in ranked]
    table = _markdown_table(["Rank", "Feature", "Importance"], rows)
    return f"## Feature Importance ({result.method})\n\n{table}"


def _render_model_comparison(result: ModelComparisonResult) -> str:
    rows = [
        [
            (
                f"**{candidate.model_name}**"
                if candidate.model_name == result.best_model_name
                else candidate.model_name
            ),
            f"{candidate.metric_value:.6g}",
            f"{candidate.training_duration_seconds:.3g}",
        ]
        for candidate in result.candidates
    ]
    table = _markdown_table(["Model", result.comparison_metric, "Training (s)"], rows)
    return f"## Model Comparison\n\nBest: **{result.best_model_name}**\n\n{table}"


def _render_correlation(result: CorrelationResult) -> str:
    rows = [
        [symbol, *[f"{value:.4g}" for value in row]]
        for symbol, row in zip(result.symbols, result.matrix, strict=True)
    ]
    table = _markdown_table(["", *result.symbols], rows)
    return f"## Correlation ({result.method})\n\n{table}"


def _render_cointegration(results: Sequence[CointegrationResult]) -> str:
    rows = [
        [
            f"{result.symbol_a} / {result.symbol_b}",
            f"{result.test_statistic:.6g}",
            f"{result.p_value:.4g}",
            "Yes" if result.is_cointegrated else "No",
            f"{result.hedge_ratio:.6g}" if result.hedge_ratio is not None else "-",
        ]
        for result in results
    ]
    table = _markdown_table(
        ["Pair", "Test statistic", "P-value", "Cointegrated", "Hedge ratio"], rows
    )
    return f"## Cointegration\n\n{table}"


def _render_pca(result: PCAResult) -> str:
    rows = [
        [str(component.component_index), f"{component.explained_variance_ratio:.4g}"]
        for component in result.components
    ]
    table = _markdown_table(["Component", "Explained variance ratio"], rows)
    return (
        f"## PCA\n\nCumulative explained variance: "
        f"{result.cumulative_explained_variance:.4g}\n\n{table}"
    )


def _render_clustering(result: ClusteringResult) -> str:
    rows = [[assignment.symbol, str(assignment.cluster_id)] for assignment in result.assignments]
    table = _markdown_table(["Symbol", "Cluster"], rows)
    silhouette = f"{result.silhouette_score:.4g}" if result.silhouette_score is not None else "n/a"
    return (
        f"## Clustering ({result.method})\n\n"
        f"Clusters: {result.cluster_count}, silhouette score: {silhouette}\n\n{table}"
    )


def _render_regime_analysis(result: RegimeAnalysisResult) -> str:
    period_rows = [
        [str(period.regime), period.start.isoformat(), period.end.isoformat()]
        for period in result.periods
    ]
    periods_table = _markdown_table(["Regime", "Start", "End"], period_rows)

    frequency_rows = [
        [str(regime), f"{percent:.2f}%"]
        for regime, percent in result.regime_frequency_percent.items()
    ]
    frequency_table = _markdown_table(["Regime", "Frequency"], frequency_rows)

    return (
        f"## Regime Analysis ({result.symbol})\n\n"
        f"{periods_table}\n\n"
        f"### Regime Frequency\n\n{frequency_table}"
    )


def _render_monte_carlo(result: MonteCarloResult) -> str:
    rows = [
        ["Simulations", str(result.simulation_count)],
        ["Expected drawdown", f"{result.expected_drawdown_percent:.2f}%"],
        ["Risk of ruin", f"{result.risk_of_ruin_percent:.2f}%"],
    ]
    summary_table = _markdown_table(["Metric", "Value"], rows)

    percentile_rows = [
        [percentile, f"{value:.4g}"] for percentile, value in result.return_percentiles.items()
    ]
    percentile_table = _markdown_table(["Percentile", "Return"], percentile_rows)

    return f"## Monte Carlo\n\n{summary_table}\n\n### Return Percentiles\n\n{percentile_table}"


def _render_walk_forward(result: WalkForwardReport) -> str:
    rows = [
        [
            str(window.window_index),
            window.train_start.isoformat(),
            window.train_end.isoformat(),
            window.validation_start.isoformat(),
            window.validation_end.isoformat(),
            f"{window.train_objective_value:.6g}",
            f"{window.validation_objective_value:.6g}",
        ]
        for window in result.windows
    ]
    table = _markdown_table(
        [
            "Window",
            "Train start",
            "Train end",
            "Validation start",
            "Validation end",
            "Train",
            "Validation",
        ],
        rows,
    )
    return (
        f"## Walk-Forward Analysis\n\n"
        f"Average validation objective: {result.average_validation_objective:.6g}, "
        f"objective degradation: {result.objective_degradation_percent:.2f}%\n\n{table}"
    )


def _render_optimization(result: HyperparameterOptimizationResult) -> str:
    best_params = ", ".join(
        f"{name}={value:.6g}" for name, value in result.best_trial.parameters.items()
    )
    rows = [
        ["Trials", str(len(result.trials))],
        ["Best objective", f"{result.best_trial.objective_value:.6g}"],
        ["Best parameters", best_params],
        ["Duration (s)", f"{result.total_duration_seconds:.3g}"],
    ]
    table = _markdown_table(["Metric", "Value"], rows)
    return f"## Hyperparameter Optimization\n\n{table}"
