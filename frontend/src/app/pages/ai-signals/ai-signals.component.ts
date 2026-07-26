import { CommonModule } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Component, DestroyRef, inject, signal } from '@angular/core';
import { catchError, forkJoin, of, switchMap } from 'rxjs';

import { ApiService } from '../../core/api.service';
import { InstructorRecommendation } from '../../core/models';
import { pollEvery } from '../../core/polling';

interface SignalRow {
  symbol: string;
  recommendation: InstructorRecommendation | null;
  unavailable: boolean;
}

@Component({
  selector: 'app-ai-signals',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './ai-signals.component.html',
  styleUrl: './ai-signals.component.scss'
})
export class AiSignalsComponent {
  private readonly api = inject(ApiService);
  private readonly destroyRef = inject(DestroyRef);

  readonly rows = signal<SignalRow[]>([]);
  readonly symbols = signal<string[]>([]);
  readonly error = signal<string | null>(null);

  constructor() {
    pollEvery(() =>
      this.api.getAutoStatus().pipe(
        switchMap((status) => {
          this.symbols.set(status.symbols);
          if (status.symbols.length === 0) return of([]);
          return forkJoin(
            status.symbols.map((symbol) =>
              this.api.getSignal('NSE', symbol).pipe(
                switchMap((recommendation) =>
                  of<SignalRow>({ symbol, recommendation, unavailable: false })
                ),
                catchError(() => of<SignalRow>({ symbol, recommendation: null, unavailable: true }))
              )
            )
          );
        })
      )
    )
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (rows) => {
          this.error.set(null);
          this.rows.set(rows);
        },
        error: (err) => this.error.set(err instanceof Error ? err.message : 'Failed to load.')
      });
  }
}
