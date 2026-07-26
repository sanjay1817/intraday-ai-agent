"""Monte Carlo Analysis: randomized resampling of a trade-outcome history
to estimate strategy robustness — expected drawdown, confidence
intervals, return distributions, and risk of ruin.

Uses bootstrap resampling (sampling with replacement) over the *order*
of historical trade outcomes, not their magnitudes: this tests whether a
strategy's apparent performance depends on a lucky sequence of wins and
losses, not whether the outcomes themselves are realistic (which the
original trade history already determined).

Takes per-trade absolute profit/loss (e.g.
`app.analytics.dto.PortfolioTradeRecord.net_pnl`), not pre-computed
percentage returns — `starting_capital` genuinely affects every output
metric this way, since drawdown/return are both reported as percentages
of it. Percentage returns computed against a starting_capital would make
that parameter a no-op (it would cancel out of every ratio), which would
be dead code, not a real configuration knob.
"""

from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

from app.domain.exceptions.research import ResearchError
from app.research.models import MonteCarloResult

#: Percentiles reported for both the drawdown and return distributions.
_REPORTED_PERCENTILES = (5.0, 50.0, 95.0)


def run_monte_carlo(
    trade_pnl: Sequence[float],
    *,
    simulation_count: int = 1000,
    starting_capital: float = 100_000.0,
    ruin_threshold_percent: float = 50.0,
    seed: int | None = None,
) -> MonteCarloResult:
    """Bootstrap-resample `trade_pnl` `simulation_count` times, each time
    replaying all `len(trade_pnl)` trades in a random order (sampled
    with replacement) against `starting_capital`, and aggregate the
    resulting equity curves.

    Args:
        trade_pnl: Per-trade absolute profit/loss, in whatever order
            they actually occurred. Only the *set* of outcomes is
            resampled — reshuffled trade order, not fabricated outcomes.
        simulation_count: How many random reorderings to simulate.
        starting_capital: Account equity each simulation begins from.
        ruin_threshold_percent: A simulation "breaches ruin" if its
            equity ever falls to this percentage of `starting_capital`
            or below (e.g. `50.0` means "lost half the account") at any
            point along the path, not just at the end.
        seed: Reproducibility seed.

    Raises:
        ResearchError: `trade_pnl` is empty, `simulation_count`/
            `starting_capital` isn't positive, or `ruin_threshold_percent`
            isn't in `(0, 100]`.
    """

    if not trade_pnl:
        raise ResearchError("Monte Carlo simulation requires at least one trade")
    if simulation_count <= 0:
        raise ResearchError("simulation_count must be positive")
    if starting_capital <= 0:
        raise ResearchError("starting_capital must be positive")
    if not 0 < ruin_threshold_percent <= 100:
        raise ResearchError("ruin_threshold_percent must be in (0, 100]")

    rng = np.random.default_rng(seed)
    pnl = np.array(trade_pnl, dtype=float)
    trade_count = len(pnl)
    ruin_level = starting_capital * ruin_threshold_percent / 100.0

    max_drawdowns = np.empty(simulation_count)
    final_returns = np.empty(simulation_count)
    ruin_breached = np.zeros(simulation_count, dtype=bool)

    for sim_index in range(simulation_count):
        sampled_indices = rng.integers(0, trade_count, size=trade_count)
        shuffled_pnl = pnl[sampled_indices]

        equity = np.concatenate(([starting_capital], starting_capital + np.cumsum(shuffled_pnl)))
        running_peak = np.maximum.accumulate(equity)  # always >= starting_capital > 0
        drawdown = (running_peak - equity) / running_peak

        max_drawdowns[sim_index] = drawdown.max() * 100.0
        final_returns[sim_index] = (equity[-1] / starting_capital - 1.0) * 100.0
        ruin_breached[sim_index] = bool((equity <= ruin_level).any())

    return MonteCarloResult(
        simulation_count=simulation_count,
        expected_drawdown_percent=float(max_drawdowns.mean()),
        drawdown_confidence_intervals=_percentile_dict(max_drawdowns),
        return_percentiles=_percentile_dict(final_returns),
        risk_of_ruin_percent=float(100.0 * ruin_breached.mean()),
    )


def _percentile_dict(values: npt.NDArray[np.float64]) -> dict[str, float]:
    return {
        f"{percentile:g}%": float(np.percentile(values, percentile))
        for percentile in _REPORTED_PERCENTILES
    }
