"""Statistical Analysis: correlation, cointegration, PCA, and clustering
over symbol return/feature histories.

Correlation is computed with pandas alone — no extra dependency needed.
Cointegration uses `statsmodels`; PCA and clustering use `scikit-learn`
— both optional 'research' extras, imported lazily inside the functions
that need them so importing this module (and calling `compute_correlation`)
never requires either installed.
"""

from typing import Literal

import numpy as np
import pandas as pd

from app.domain.exceptions.research import ResearchError
from app.research.models import (
    ClusterAssignment,
    ClusteringResult,
    CointegrationResult,
    CorrelationResult,
    PCAComponent,
    PCAResult,
)

#: Below this many overlapping observations, an Engle-Granger
#: cointegration test is statistically meaningless regardless of what
#: p-value it happens to produce.
_MIN_COINTEGRATION_OBSERVATIONS = 20

#: The conventional 5% significance threshold for "is this pair
#: cointegrated" — configurable per call would be premature given
#: nothing in this project has asked for anything else yet.
_COINTEGRATION_SIGNIFICANCE = 0.05


def compute_correlation(
    returns: pd.DataFrame, method: Literal["pearson", "spearman", "kendall"] = "pearson"
) -> CorrelationResult:
    """Compute the symbol-by-symbol correlation matrix of `returns`.

    Args:
        returns: Columns are symbols, index is time, values are period
            returns (not price levels — correlate returns, not prices,
            or the result is dominated by shared trend rather than
            genuine co-movement).
        method: `pandas.DataFrame.corr`'s method — Pearson (linear),
            Spearman or Kendall (rank-based, robust to outliers/non-linearity).
    """

    if returns.shape[1] < 2:
        raise ResearchError("correlation requires at least 2 symbols (columns)")

    matrix = returns.corr(method=method)
    symbols = list(matrix.columns)
    values = matrix.to_numpy()

    return CorrelationResult(
        symbols=symbols,
        matrix=[[float(value) for value in row] for row in values],
        method=method,
    )


def test_cointegration(
    prices_a: pd.Series, prices_b: pd.Series, symbol_a: str, symbol_b: str
) -> CointegrationResult:
    """Engle-Granger cointegration test between two *price* series.

    Cointegration is a level-series concept — pass price levels, not
    returns (unlike `compute_correlation`, which wants returns).

    Raises:
        ResearchError: `statsmodels` isn't installed, or the two series
            have fewer than `_MIN_COINTEGRATION_OBSERVATIONS` overlapping
            observations after aligning on their shared index.
    """

    try:
        from statsmodels.tsa.stattools import coint
    except ImportError as exc:
        raise ResearchError(
            "cointegration testing requires the optional 'statsmodels' package "
            "(install with the 'research' extra)"
        ) from exc

    aligned_a, aligned_b = prices_a.align(prices_b, join="inner")
    if len(aligned_a) < _MIN_COINTEGRATION_OBSERVATIONS:
        raise ResearchError(
            f"cointegration test needs at least {_MIN_COINTEGRATION_OBSERVATIONS} overlapping "
            f"observations, got {len(aligned_a)}"
        )

    test_statistic, p_value, critical_values = coint(aligned_a, aligned_b)

    return CointegrationResult(
        symbol_a=symbol_a,
        symbol_b=symbol_b,
        test_statistic=float(test_statistic),
        p_value=float(p_value),
        critical_values={
            "1%": float(critical_values[0]),
            "5%": float(critical_values[1]),
            "10%": float(critical_values[2]),
        },
        is_cointegrated=bool(p_value < _COINTEGRATION_SIGNIFICANCE),
        hedge_ratio=_ols_hedge_ratio(aligned_a, aligned_b),
    )


def _ols_hedge_ratio(series_a: pd.Series, series_b: pd.Series) -> float:
    """The OLS slope of `series_a` regressed on `series_b` — the
    conventional hedge ratio for a cointegrated pair (how many units of
    B offset one unit of A).
    """

    b_with_intercept = np.column_stack([np.ones(len(series_b)), series_b.to_numpy()])
    coefficients, _residuals, _rank, _singular_values = np.linalg.lstsq(
        b_with_intercept, series_a.to_numpy(), rcond=None
    )
    return float(coefficients[1])


def compute_pca(features: pd.DataFrame, n_components: int | None = None) -> PCAResult:
    """Principal Component Analysis over `features`.

    Args:
        features: Columns are named features, index is observations.
            Rows with any missing value are dropped before fitting —
            PCA has no defined behavior for `NaN`.
        n_components: How many components to retain; defaults to the
            maximum possible (`min(rows, columns)`).

    Raises:
        ResearchError: `scikit-learn` isn't installed, every row has a
            missing value, or `n_components` exceeds the maximum possible.
    """

    try:
        from sklearn.decomposition import PCA
    except ImportError as exc:
        raise ResearchError(
            "PCA requires the optional 'scikit-learn' package (install with the 'research' extra)"
        ) from exc

    clean = features.dropna()
    if clean.empty:
        raise ResearchError("PCA requires at least one row with no missing values")

    max_components = min(clean.shape[0], clean.shape[1])
    resolved_components = n_components or max_components
    if resolved_components > max_components:
        raise ResearchError(
            f"n_components={resolved_components} exceeds the maximum of {max_components} "
            f"for {clean.shape[0]} rows and {clean.shape[1]} columns"
        )

    model = PCA(n_components=resolved_components)
    model.fit(clean.to_numpy())

    feature_names = list(clean.columns)
    components = [
        PCAComponent(
            component_index=index,
            explained_variance_ratio=float(ratio),
            loadings=dict(zip(feature_names, (float(value) for value in loading_row), strict=True)),
        )
        for index, (ratio, loading_row) in enumerate(
            zip(model.explained_variance_ratio_, model.components_, strict=True)
        )
    ]

    return PCAResult(
        components=components,
        cumulative_explained_variance=float(model.explained_variance_ratio_.sum()),
    )


def cluster_symbols(
    features: dict[str, list[float]], n_clusters: int, *, random_state: int | None = None
) -> ClusteringResult:
    """K-means clustering of symbols by their feature profile.

    Args:
        features: One feature vector per symbol (e.g. return, volatility,
            and correlation-derived statistics — whatever the caller
            supplies; this function has no opinion on what a "feature"
            means here, only that every symbol's vector is the same length).
        n_clusters: How many clusters to form.
        random_state: Reproducibility seed for k-means' initialization.

    Raises:
        ResearchError: `scikit-learn` isn't installed, fewer than 2
            symbols were supplied, or `n_clusters` is out of range.
    """

    try:
        from sklearn.cluster import KMeans
        from sklearn.metrics import silhouette_score
    except ImportError as exc:
        raise ResearchError(
            "clustering requires the optional 'scikit-learn' package "
            "(install with the 'research' extra)"
        ) from exc

    symbols = list(features)
    if len(symbols) < 2:
        raise ResearchError("clustering requires at least 2 symbols")
    if not 1 <= n_clusters <= len(symbols):
        raise ResearchError(f"n_clusters must be between 1 and {len(symbols)} (got {n_clusters})")

    matrix = np.array([features[symbol] for symbol in symbols])
    model = KMeans(n_clusters=n_clusters, n_init="auto", random_state=random_state)
    labels = model.fit_predict(matrix)

    # Silhouette score is undefined for 1 cluster or one-cluster-per-point.
    silhouette = float(silhouette_score(matrix, labels)) if 1 < n_clusters < len(symbols) else None

    return ClusteringResult(
        method="kmeans",
        assignments=[
            ClusterAssignment(symbol=symbol, cluster_id=int(label))
            for symbol, label in zip(symbols, labels, strict=True)
        ],
        cluster_count=n_clusters,
        silhouette_score=silhouette,
    )
