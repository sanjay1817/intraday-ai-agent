import { CommonModule } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Component, DestroyRef, computed, inject, signal } from '@angular/core';

import { ApiService } from '../../core/api.service';
import { LogEntry } from '../../core/models';
import { pollEvery } from '../../core/polling';

@Component({
  selector: 'app-logs',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './logs.component.html',
  styleUrl: './logs.component.scss'
})
export class LogsComponent {
  private readonly api = inject(ApiService);
  private readonly destroyRef = inject(DestroyRef);

  readonly logs = signal<LogEntry[]>([]);
  readonly error = signal<string | null>(null);
  readonly levelFilter = signal<string>('ALL');

  readonly levels = ['ALL', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];

  constructor() {
    pollEvery(() => this.api.getLogs(500))
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (logs) => {
          this.error.set(null);
          this.logs.set([...logs].reverse());
        },
        error: (err) => this.error.set(err instanceof Error ? err.message : 'Failed to load.')
      });
  }

  setFilter(level: string): void {
    this.levelFilter.set(level);
  }

  readonly filtered = computed(() => {
    const level = this.levelFilter();
    return level === 'ALL' ? this.logs() : this.logs().filter((l) => l.level === level);
  });
}
