import { CommonModule } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Component, DestroyRef, computed, inject, signal } from '@angular/core';
import { combineLatest } from 'rxjs';

import { ApiService } from '../../core/api.service';
import { AutoTradingStatus, ClosedTrade, Portfolio } from '../../core/models';
import { pollEvery } from '../../core/polling';
import { BarChartComponent, BarPoint } from '../../shared/charts/bar-chart.component';
import { DonutChartComponent, DonutSlice } from '../../shared/charts/donut-chart.component';
import { LineChartComponent, LinePoint } from '../../shared/charts/line-chart.component';

const MAX_EQUITY_POINTS = 90;

@Component({
  selector: 'app-overview',
  standalone: true,
  imports: [CommonModule, LineChartComponent, BarChartComponent, DonutChartComponent],
  templateUrl: './overview.component.html',
  styleUrl: './overview.component.scss'
})
export class OverviewComponent {
  private readonly api = inject(ApiService);
  private readonly destroyRef = inject(DestroyRef);

  readonly portfolio = signal<Portfolio | null>(null);
  readonly autoStatus = signal<AutoTradingStatus | null>(null);
  readonly trades = signal<ClosedTrade[]>([]);
  readonly equityHistory = signal<LinePoint[]>([]);
  readonly error = signal<string | null>(null);

  readonly dailyPnl = computed<BarPoint[]>(() => {
    const byDay = new Map<string, number>();
    for (const trade of this.trades()) {
      const day = trade.exit_timestamp.slice(0, 10);
      byDay.set(day, (byDay.get(day) ?? 0) + trade.pnl);
    }
    return [...byDay.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .slice(-14)
      .map(([label, value]) => ({ label, value }));
  });

  readonly winLossSlices = computed<DonutSlice[]>(() => {
    const wins = this.trades().filter((t) => t.pnl > 0).length;
    const losses = this.trades().filter((t) => t.pnl <= 0).length;
    return [
      { label: 'Wins', value: wins, color: 'var(--green)' },
      { label: 'Losses', value: losses, color: 'var(--red)' }
    ];
  });

  readonly tradeDistribution = computed<BarPoint[]>(() => {
    const bySymbol = new Map<string, number>();
    for (const trade of this.trades()) {
      bySymbol.set(trade.symbol, (bySymbol.get(trade.symbol) ?? 0) + 1);
    }
    return [...bySymbol.entries()]
      .sort(([, a], [, b]) => b - a)
      .slice(0, 10)
      .map(([label, value]) => ({ label, value }));
  });

  constructor() {
    pollEvery(() =>
      combineLatest([this.api.getPortfolio(), this.api.getAutoStatus(), this.api.getTrades()])
    )
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: ([portfolio, autoStatus, trades]) => {
          this.error.set(null);
          this.portfolio.set(portfolio);
          this.autoStatus.set(autoStatus);
          this.trades.set(trades);
          this.pushEquityPoint(portfolio);
        },
        error: (err) => {
          this.error.set(err instanceof Error ? err.message : 'Failed to reach the backend.');
        }
      });
  }

  private pushEquityPoint(portfolio: Portfolio): void {
    const label = new Date(portfolio.updated_at).toLocaleTimeString();
    this.equityHistory.update((history) => {
      const next = [...history, { label, value: portfolio.equity }];
      return next.length > MAX_EQUITY_POINTS ? next.slice(-MAX_EQUITY_POINTS) : next;
    });
  }
}
