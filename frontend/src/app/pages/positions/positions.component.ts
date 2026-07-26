import { CommonModule } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Component, DestroyRef, inject, signal } from '@angular/core';

import { ApiService } from '../../core/api.service';
import { PaperPosition } from '../../core/models';
import { pollEvery } from '../../core/polling';

@Component({
  selector: 'app-positions',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './positions.component.html',
  styleUrl: './positions.component.scss'
})
export class PositionsComponent {
  private readonly api = inject(ApiService);
  private readonly destroyRef = inject(DestroyRef);

  readonly positions = signal<PaperPosition[]>([]);
  readonly error = signal<string | null>(null);

  constructor() {
    pollEvery(() => this.api.getPositions())
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (positions) => {
          this.error.set(null);
          this.positions.set(positions);
        },
        error: (err) => this.error.set(err instanceof Error ? err.message : 'Failed to load.')
      });
  }
}
