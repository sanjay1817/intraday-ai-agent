import { CommonModule } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Component, DestroyRef, inject, signal } from '@angular/core';

import { ApiService } from '../../core/api.service';
import { AutoTradingStatus } from '../../core/models';
import { pollEvery } from '../../core/polling';

@Component({
  selector: 'app-auto-status',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './auto-status.component.html',
  styleUrl: './auto-status.component.scss'
})
export class AutoStatusComponent {
  private readonly api = inject(ApiService);
  private readonly destroyRef = inject(DestroyRef);

  readonly status = signal<AutoTradingStatus | null>(null);
  readonly error = signal<string | null>(null);

  constructor() {
    pollEvery(() => this.api.getAutoStatus())
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (status) => {
          this.error.set(null);
          this.status.set(status);
        },
        error: (err) => this.error.set(err instanceof Error ? err.message : 'Failed to load.')
      });
  }
}
