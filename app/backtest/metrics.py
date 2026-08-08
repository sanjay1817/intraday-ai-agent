"""Backtest performance metrics: pure functions over a completed
replay's trade records and equity curve — no I/O, no broker/engine
dependency, easy to test deterministically against a hand-built trade
list.
"""

from app.backtest.dto import BacktestSummary, BacktestTradeRecord, EquityPoint


def summarize(
    trades: list[BacktestTradeRecord], equity_curve: list[EquityPoint], *, initial_capital: float
) -> BacktestSummary:
    """Compute every statistic the feature's reporting requirements
    call for from one replay's completed trades and equity curve.
    """

    final_capital = equity_curve[-1].equity if equity_curve else initial_capital
    total_pnl = final_capital - initial_capital
    total_pnl_percent = (total_pnl / initial_capital * 100.0) if initial_capital > 0 else 0.0

    wins = [trade.net_pnl for trade in trades if trade.net_pnl > 0]
    losses = [trade.net_pnl for trade in trades if trade.net_pnl < 0]

    total_trades = len(trades)
    winning_trades = len(wins)
    losing_trades = len(losses)
    win_rate = (winning_trades / total_trades * 100.0) if total_trades > 0 else 0.0

    average_profit = sum(wins) / len(wins) if wins else 0.0
    average_loss = sum(losses) / len(losses) if losses else 0.0
    largest_win = max(wins) if wins else 0.0
    largest_loss = min(losses) if losses else 0.0

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    max_drawdown = max((point.drawdown for point in equity_curve), default=0.0)
    max_drawdown_percent = max((point.drawdown_percent for point in equity_curve), default=0.0)

    return BacktestSummary(
        initial_capital=initial_capital,
        final_capital=final_capital,
        total_pnl=total_pnl,
        total_pnl_percent=total_pnl_percent,
        total_trades=total_trades,
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        win_rate=win_rate,
        average_profit=average_profit,
        average_loss=average_loss,
        largest_win=largest_win,
        largest_loss=largest_loss,
        max_drawdown=max_drawdown,
        max_drawdown_percent=max_drawdown_percent,
        profit_factor=profit_factor,
    )
