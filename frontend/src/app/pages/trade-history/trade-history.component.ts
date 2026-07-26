import { CommonModule } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Component, DestroyRef, inject, signal } from '@angular/core';

import { ApiService } from '../../core/api.service';
import { ClosedTrade } from '../../core/models';
import { pollEvery } from '../../core/polling';

@Component({
  selector: 'app-trade-history',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './trade-history.component.html',
  styleUrl: './trade-history.component.scss'
})
export class TradeHistoryComponent {
  private readonly api = inject(ApiService);
  private readonly destroyRef = inject(DestroyRef);

  readonly trades = signal<ClosedTrade[]>([]);
  readonly error = signal<string | null>(null);

  constructor() {
    pollEvery(() => this.api.getTrades())
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (trades) => {
          this.error.set(null);
          this.trades.set(
            [...trades].sort((a, b) => b.exit_timestamp.localeCompare(a.exit_timestamp))
          );
        },
        error: (err) => this.error.set(err instanceof Error ? err.message : 'Failed to load.')
      });
  }
}
