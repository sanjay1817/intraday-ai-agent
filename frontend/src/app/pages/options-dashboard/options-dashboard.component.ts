import { CommonModule } from '@angular/common';
import { toObservable, takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Component, DestroyRef, inject, signal, computed } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { catchError, of, switchMap } from 'rxjs';

import { ApiService } from '../../core/api.service';
import {
  AutoTradingStatus,
  HealthResponse,
  OptionAutoTradingStatus,
  OptionRecommendation,
  OptionRiskStatus,
  OptionTradeHistoryEntry,
  PaperPosition,
  Portfolio
} from '../../core/models';
import { pollEvery } from '../../core/polling';

// A sane default shown before the first `/health` poll resolves --
// replaced with `health().option_underlyings` (the real configured
// list) as soon as that arrives, so the underlying selector is never
// left empty.
const DEFAULT_UNDERLYINGS = ['NIFTY', 'BANKNIFTY', 'FINNIFTY'];

@Component({
  selector: 'app-options-dashboard',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './options-dashboard.component.html',
  styleUrl: './options-dashboard.component.scss'
})
export class OptionsDashboardComponent {
  private readonly api = inject(ApiService);
  private readonly destroyRef = inject(DestroyRef);

  readonly underlyings = signal<string[]>(DEFAULT_UNDERLYINGS);
  readonly selectedUnderlying = signal<string>('NIFTY');

  readonly health = signal<HealthResponse | null>(null);
  readonly healthError = signal<string | null>(null);

  readonly recommendation = signal<OptionRecommendation | null>(null);
  readonly recommendationError = signal<string | null>(null);

  readonly portfolio = signal<Portfolio | null>(null);
  readonly portfolioError = signal<string | null>(null);

  readonly optionPositions = signal<PaperPosition[]>([]);

  readonly autoStatus = signal<AutoTradingStatus | null>(null);

  readonly trades = signal<OptionTradeHistoryEntry[]>([]);
  readonly tradesError = signal<string | null>(null);

  readonly riskStatus = signal<OptionRiskStatus | null>(null);
  readonly riskError = signal<string | null>(null);

  readonly optionAutoStatus = signal<OptionAutoTradingStatus | null>(null);
  readonly optionAutoError = signal<string | null>(null);

  // Portfolio Summary section: `Portfolio` has no day-scoped whole-
  // portfolio P&L field (`total_pnl` is cumulative since the last paper
  // reset) -- Return % is likewise not returned by the backend, so both
  // are derived here client-side rather than fabricated as new backend
  // fields.
  readonly returnPercent = computed(() => {
    const p = this.portfolio();
    if (!p || p.initial_capital === 0) return null;
    return (p.total_pnl / p.initial_capital) * 100;
  });

  // "Paper Trading Status": derived from whether the portfolio poll is
  // currently succeeding -- no new backend field needed for this.
  readonly paperTradingActive = computed(
    () => this.portfolioError() === null && this.portfolio() !== null
  );

  constructor() {
    pollEvery(() => this.api.getHealth())
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (health) => {
          this.healthError.set(null);
          this.health.set(health);
          if (health.option_underlyings.length > 0) {
            this.underlyings.set(health.option_underlyings);
            if (!health.option_underlyings.includes(this.selectedUnderlying())) {
              this.selectedUnderlying.set(health.option_underlyings[0]);
            }
          }
        },
        error: (err) =>
          this.healthError.set(err instanceof Error ? err.message : 'Failed to load.')
      });

    // Re-fetch whenever the selected underlying changes, in addition to
    // the normal 2-second cadence -- `switchMap` cancels any in-flight
    // request for a previous underlying.
    toObservable(this.selectedUnderlying)
      .pipe(
        switchMap((underlying) =>
          pollEvery(() =>
            this.api.getOptionRecommendation(underlying).pipe(
              catchError(() => of(null))
            )
          )
        ),
        takeUntilDestroyed(this.destroyRef)
      )
      .subscribe({
        next: (recommendation) => {
          if (recommendation === null) {
            this.recommendation.set(null);
            this.recommendationError.set(
              'Recommendation unavailable (options infrastructure not configured, ' +
                'unsupported underlying, or a broker/data error).'
            );
            return;
          }
          this.recommendationError.set(null);
          this.recommendation.set(recommendation);
        }
      });

    pollEvery(() => this.api.getPortfolio())
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (portfolio) => {
          this.portfolioError.set(null);
          this.portfolio.set(portfolio);
        },
        error: (err) =>
          this.portfolioError.set(err instanceof Error ? err.message : 'Failed to load.')
      });

    pollEvery(() => this.api.getPositions())
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (positions) =>
          this.optionPositions.set(positions.filter((p) => p.exchange === 'NFO')),
        error: () => this.optionPositions.set([])
      });

    pollEvery(() => this.api.getAutoStatus())
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (status) => this.autoStatus.set(status),
        error: () => this.autoStatus.set(null)
      });

    pollEvery(() => this.api.getOptionTrades())
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (trades) => {
          this.tradesError.set(null);
          this.trades.set(
            [...trades].sort((a, b) => b.exit_timestamp.localeCompare(a.exit_timestamp))
          );
        },
        error: (err) => this.tradesError.set(err instanceof Error ? err.message : 'Failed to load.')
      });

    pollEvery(() => this.api.getOptionRiskStatus())
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (risk) => {
          this.riskError.set(null);
          this.riskStatus.set(risk);
        },
        error: () => {
          this.riskStatus.set(null);
          this.riskError.set('Risk status unavailable (options infrastructure not configured).');
        }
      });

    pollEvery(() => this.api.getOptionAutoStatus())
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (status) => {
          this.optionAutoError.set(null);
          this.optionAutoStatus.set(status);
        },
        error: () => {
          this.optionAutoStatus.set(null);
          this.optionAutoError.set(
            'Auto options trading status unavailable (options infrastructure not configured, ' +
              'or TRADING_MODE is not OPTIONS).'
          );
        }
      });
  }

  onUnderlyingChange(underlying: string): void {
    this.selectedUnderlying.set(underlying);
  }

  // Lots is a client-side approximation: `PaperPosition` has no
  // `lot_size` field of its own (see `app.paper.models.PaperPosition`),
  // so this divides by the *default* configured lot size rather than
  // the actual per-contract lot size, which this endpoint doesn't
  // return. Rounded, and only ever a display estimate -- never fed back
  // into any order-sizing logic.
  approximateLots(position: PaperPosition): number {
    const lotSize = this.health()?.option_default_lot_size ?? 1;
    return Math.round(Math.abs(position.quantity) / lotSize);
  }

  // Small pure formatter -- no new dependency for "12m 34s" / "1h 05m"
  // style holding-time display.
  formatHoldingTime(seconds: number): string {
    const totalSeconds = Math.max(0, Math.round(seconds));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const secs = totalSeconds % 60;

    if (hours > 0) {
      return `${hours}h ${String(minutes).padStart(2, '0')}m`;
    }
    return `${minutes}m ${String(secs).padStart(2, '0')}s`;
  }
}
