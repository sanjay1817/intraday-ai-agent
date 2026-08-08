import { CommonModule } from '@angular/common';
import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { ApiService } from '../../core/api.service';
import { BacktestRequest, BacktestResult, Exchange, HistoricalInterval } from '../../core/models';

// Defaults for a first-time visitor: a full NSE intraday session on the
// most recent trading date before today (never today itself -- the
// backend rejects a same-day/future date, see `BacktestRequest
// .check_not_future_dated`).
function defaultHistoricalDate(): string {
  const d = new Date();
  d.setDate(d.getDate() - 1);
  return d.toISOString().slice(0, 10);
}

@Component({
  selector: 'app-backtest',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './backtest.component.html',
  styleUrl: './backtest.component.scss'
})
export class BacktestComponent {
  private readonly api = inject(ApiService);

  // -- form state -----------------------------------------------------------------------

  readonly symbol = signal('RELIANCE-EQ');
  readonly exchange = signal<Exchange>('NSE');
  readonly historicalDate = signal(defaultHistoricalDate());
  readonly startTime = signal('09:15');
  readonly endTime = signal('15:30');
  readonly interval = signal<HistoricalInterval>('1minute');
  readonly initialCapital = signal(50000);
  readonly confidenceThreshold = signal(60);

  // Today's date, for the date input's `max` attribute -- a client-side
  // convenience only; the backend is the actual source of truth for
  // "no future dates" (`FutureDateError`).
  readonly maxDate = new Date().toISOString().slice(0, 10);

  // -- run state --------------------------------------------------------------------------

  readonly running = signal(false);
  readonly error = signal<string | null>(null);
  readonly result = signal<BacktestResult | null>(null);

  runBacktest(): void {
    this.running.set(true);
    this.error.set(null);
    this.result.set(null);

    const body: BacktestRequest = {
      symbol: this.symbol().trim(),
      exchange: this.exchange(),
      historical_date: this.historicalDate(),
      start_time: `${this.startTime()}:00`,
      end_time: `${this.endTime()}:00`,
      interval: this.interval(),
      initial_capital: this.initialCapital(),
      confidence_threshold: this.confidenceThreshold()
    };

    this.api.runBacktest(body).subscribe({
      next: (result) => {
        this.running.set(false);
        this.result.set(result);
      },
      error: (err) => {
        this.running.set(false);
        const detail = err?.error?.error?.message ?? err?.message ?? 'Backtest run failed.';
        this.error.set(detail);
      }
    });
  }

  // Small client-side helper -- a `EquityPoint[]` polyline plotted as an
  // inline SVG path, no charting dependency added for one sparkline.
  equityPath(result: BacktestResult): string {
    const points = result.equity_curve;
    if (points.length === 0) return '';

    const equities = points.map((p) => p.equity);
    const min = Math.min(...equities);
    const max = Math.max(...equities);
    const range = max - min || 1;

    const width = 600;
    const height = 120;
    const step = points.length > 1 ? width / (points.length - 1) : 0;

    return points
      .map((p, i) => {
        const x = i * step;
        const y = height - ((p.equity - min) / range) * height;
        return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(' ');
  }
}
