import { CommonModule } from '@angular/common';
import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { catchError, finalize, forkJoin, map, of } from 'rxjs';

import { ApiService } from '../../core/api.service';
import {
  AggregateBacktestSummary,
  BacktestRequest,
  BacktestResult,
  Exchange,
  HistoricalInterval,
  OptionsBacktestRequest
} from '../../core/models';
import { BacktestResultComponent } from './backtest-result.component';

export interface OptionsRunOutcome {
  underlying: string;
  result: BacktestResult | null;
  error: string | null;
}

// Defaults for a first-time visitor: a full NSE intraday session on the
// most recent trading WEEKDAY before today (never today itself -- the
// backend rejects a same-day/future date, see `BacktestRequest
// .check_not_future_dated` -- and never a Sat/Sun, since NSE never
// trades then and the backend would just report zero candles).
function defaultHistoricalDate(): string {
  const d = new Date();
  d.setDate(d.getDate() - 1);
  while (d.getDay() === 0 || d.getDay() === 6) {
    d.setDate(d.getDate() - 1);
  }
  return d.toISOString().slice(0, 10);
}

function extractErrorMessage(err: unknown): string {
  const anyErr = err as { error?: { error?: { message?: string } }; message?: string };
  return anyErr?.error?.error?.message ?? anyErr?.message ?? 'Backtest run failed.';
}

@Component({
  selector: 'app-backtest',
  standalone: true,
  imports: [CommonModule, FormsModule, BacktestResultComponent],
  templateUrl: './backtest.component.html',
  styleUrl: './backtest.component.scss'
})
export class BacktestComponent {
  private readonly api = inject(ApiService);

  // -- form state -----------------------------------------------------------------------

  // Comma-separated so one click can replay a whole watchlist (e.g. the
  // 5 equity symbols this page defaults to) in a single batch run.
  readonly symbolsInput = signal('SBIN-EQ, RELIANCE-EQ, TCS-EQ, INFY-EQ, ICICIBANK-EQ');
  readonly exchange = signal<Exchange>('NSE');
  readonly historicalDate = signal(defaultHistoricalDate());
  readonly startTime = signal('09:15');
  readonly endTime = signal('15:30');
  readonly interval = signal<HistoricalInterval>('1minute');
  readonly initialCapital = signal(50000);
  readonly confidenceThreshold = signal(60);

  readonly includeOptions = signal(true);
  readonly optionUnderlyingsInput = signal('NIFTY, BANKNIFTY, FINNIFTY');

  // Today's date, for the date input's `max` attribute -- a client-side
  // convenience only; the backend is the actual source of truth for
  // "no future dates" (`FutureDateError`).
  readonly maxDate = new Date().toISOString().slice(0, 10);

  // -- run state --------------------------------------------------------------------------

  readonly running = signal(false);
  readonly error = signal<string | null>(null);

  readonly equityResults = signal<BacktestResult[]>([]);
  readonly equityAggregate = signal<AggregateBacktestSummary | null>(null);

  readonly optionsResults = signal<OptionsRunOutcome[]>([]);

  private parseList(input: string): string[] {
    return input
      .split(',')
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
  }

  runBacktest(): void {
    this.running.set(true);
    this.error.set(null);
    this.equityResults.set([]);
    this.equityAggregate.set(null);
    this.optionsResults.set([]);

    const symbols = this.parseList(this.symbolsInput());
    const underlyings = this.includeOptions() ? this.parseList(this.optionUnderlyingsInput()) : [];

    if (symbols.length === 0 && underlyings.length === 0) {
      this.running.set(false);
      this.error.set('Enter at least one symbol, or enable options with at least one underlying.');
      return;
    }

    const baseFields = {
      historical_date: this.historicalDate(),
      start_time: `${this.startTime()}:00`,
      end_time: `${this.endTime()}:00`,
      interval: this.interval(),
      initial_capital: this.initialCapital(),
      confidence_threshold: this.confidenceThreshold()
    };

    // Every symbol runs as one independent session (its own fresh
    // capital/engine) inside a single batch call -- one click replays
    // the whole watchlist and returns both per-symbol results and the
    // combined aggregate.
    const equity$ =
      symbols.length === 0
        ? of(null)
        : this.api
            .runBacktestBatch(
              symbols.map(
                (symbol): BacktestRequest => ({ symbol, exchange: this.exchange(), ...baseFields })
              )
            )
            .pipe(
              catchError((err) => {
                this.error.set(extractErrorMessage(err));
                return of(null);
              })
            );

    // Options runs are independent per underlying -- one underlying's
    // failure (e.g. no historical data for the resolved contract) must
    // never block the others from completing.
    const options$ =
      underlyings.length === 0
        ? of([] as OptionsRunOutcome[])
        : forkJoin(
            underlyings.map((underlying) => {
              const request: OptionsBacktestRequest = {
                symbol: underlying,
                exchange: this.exchange(),
                ...baseFields,
                strike_mode: 'ATM',
                expiry_mode: 'NEAREST_WEEKLY'
              };
              return this.api.runOptionsBacktest(request).pipe(
                map((result): OptionsRunOutcome => ({ underlying, result, error: null })),
                catchError((err) =>
                  of<OptionsRunOutcome>({ underlying, result: null, error: extractErrorMessage(err) })
                )
              );
            })
          );

    forkJoin([equity$, options$]).pipe(finalize(() => this.running.set(false))).subscribe({
      next: ([aggregate, optionOutcomes]) => {
        if (aggregate) {
          this.equityResults.set(aggregate.session_results);
          this.equityAggregate.set(aggregate.aggregate);
        }
        this.optionsResults.set(optionOutcomes);
      }
    });
  }
}
