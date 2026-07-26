import { CommonModule } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Component, DestroyRef, inject, signal } from '@angular/core';

import { ApiService } from '../../core/api.service';
import { PaperOrder } from '../../core/models';
import { pollEvery } from '../../core/polling';

@Component({
  selector: 'app-orders',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './orders.component.html',
  styleUrl: './orders.component.scss'
})
export class OrdersComponent {
  private readonly api = inject(ApiService);
  private readonly destroyRef = inject(DestroyRef);

  readonly orders = signal<PaperOrder[]>([]);
  readonly error = signal<string | null>(null);

  constructor() {
    pollEvery(() => this.api.getOrders())
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (orders) => {
          this.error.set(null);
          this.orders.set([...orders].sort((a, b) => b.created_at.localeCompare(a.created_at)));
        },
        error: (err) => this.error.set(err instanceof Error ? err.message : 'Failed to load.')
      });
  }
}
