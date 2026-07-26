import { CommonModule } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Component, DestroyRef, inject, signal } from '@angular/core';

import { ApiService } from '../../core/api.service';
import { Portfolio } from '../../core/models';
import { pollEvery } from '../../core/polling';

@Component({
  selector: 'app-portfolio',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './portfolio.component.html',
  styleUrl: './portfolio.component.scss'
})
export class PortfolioComponent {
  private readonly api = inject(ApiService);
  private readonly destroyRef = inject(DestroyRef);

  readonly portfolio = signal<Portfolio | null>(null);
  readonly error = signal<string | null>(null);

  constructor() {
    pollEvery(() => this.api.getPortfolio())
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (portfolio) => {
          this.error.set(null);
          this.portfolio.set(portfolio);
        },
        error: (err) => this.error.set(err instanceof Error ? err.message : 'Failed to load.')
      });
  }
}
