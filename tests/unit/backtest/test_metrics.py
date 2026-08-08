"""Unit tests for `app.backtest.metrics.summarize`."""

from datetime import UTC, datetime, timedelta

from app.backtest.dto import BacktestTradeRecord, EquityPoint
from app.backtest.metrics import summarize
from app.domain.enums.trading import Exchange, OrderSide

_T0 = datetime(2026, 8, 5, 9, 15, tzinfo=UTC)


def _trade(net_pnl: float) -> BacktestTradeRecord:
    return BacktestTradeRecord(
        symbol="RELIANCE-EQ",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        quantity=10,
        entry_time=_T0,
        entry_signal_price=100.0,
        entry_fill_price=100.0,
        exit_time=_T0 + timedelta(minutes=5),
        exit_signal_price=100.0 + net_pnl / 10,
        exit_fill_price=100.0 + net_pnl / 10,
        exit_reason="target",
        gross_pnl=net_pnl,
        charges=0.0,
        slippage_cost=0.0,
        net_pnl=net_pnl,
    )


def _equity_point(equity: float, drawdown: float = 0.0) -> EquityPoint:
    drawdown_percent = (drawdown / (equity + drawdown) * 100.0) if (equity + drawdown) > 0 else 0.0
    return EquityPoint(timestamp=_T0, equity=equity, drawdown=drawdown, drawdown_percent=drawdown_percent)


def test_summarize_with_no_trades_reports_zeroed_stats() -> None:
    summary = summarize([], [], initial_capital=50_000.0)

    assert summary.total_trades == 0
    assert summary.winning_trades == 0
    assert summary.losing_trades == 0
    assert summary.win_rate == 0.0
    assert summary.final_capital == 50_000.0
    assert summary.total_pnl == 0.0
    assert summary.profit_factor is None


def test_summarize_computes_win_rate_and_averages() -> None:
    trades = [_trade(100.0), _trade(-50.0), _trade(200.0), _trade(-25.0)]
    equity_curve = [_equity_point(50_225.0)]

    summary = summarize(trades, equity_curve, initial_capital=50_000.0)

    assert summary.total_trades == 4
    assert summary.winning_trades == 2
    assert summary.losing_trades == 2
    assert summary.win_rate == 50.0
    assert summary.average_profit == 150.0
    assert summary.average_loss == -37.5
    assert summary.largest_win == 200.0
    assert summary.largest_loss == -50.0


def test_profit_factor_is_gross_profit_over_gross_loss() -> None:
    trades = [_trade(300.0), _trade(-100.0)]

    summary = summarize(trades, [_equity_point(50_200.0)], initial_capital=50_000.0)

    assert summary.profit_factor == 3.0


def test_max_drawdown_taken_from_the_equity_curve() -> None:
    equity_curve = [
        _equity_point(50_000.0, drawdown=0.0),
        _equity_point(49_000.0, drawdown=1_000.0),
        _equity_point(49_500.0, drawdown=500.0),
    ]

    summary = summarize([], equity_curve, initial_capital=50_000.0)

    assert summary.max_drawdown == 1_000.0


def test_total_pnl_percent_matches_initial_capital_ratio() -> None:
    summary = summarize([], [_equity_point(55_000.0)], initial_capital=50_000.0)

    assert summary.total_pnl == 5_000.0
    assert summary.total_pnl_percent == 10.0
