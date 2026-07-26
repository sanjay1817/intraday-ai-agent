import { CommonModule } from '@angular/common';
import { Component, signal } from '@angular/core';

import { ApiService } from '../../core/api.service';
import { ConfirmDialogComponent } from '../confirm-dialog/confirm-dialog.component';

type PendingAction = 'emergency-stop' | 'reset' | null;

@Component({
  selector: 'app-controls-bar',
  standalone: true,
  imports: [CommonModule, ConfirmDialogComponent],
  templateUrl: './controls-bar.component.html',
  styleUrl: './controls-bar.component.scss'
})
export class ControlsBarComponent {
  readonly running = signal(false);
  readonly busy = signal(false);
  readonly pending = signal<PendingAction>(null);
  readonly lastMessage = signal<string | null>(null);
  readonly lastError = signal<string | null>(null);

  constructor(private readonly api: ApiService) {
    this.api.getAutoStatus().subscribe({
      next: (status) => this.running.set(status.running),
      error: () => {}
    });
  }

  startAutoTrading(): void {
    this.busy.set(true);
    this.api.startAutoTrading().subscribe({
      next: (status) => {
        this.running.set(status.running);
        this.busy.set(false);
        this.announce('Auto trading started.');
      },
      error: (err) => this.fail(err)
    });
  }

  stopAutoTrading(): void {
    this.busy.set(true);
    this.api.stopAutoTrading().subscribe({
      next: (status) => {
        this.running.set(status.running);
        this.busy.set(false);
        this.announce('Auto trading stopped.');
      },
      error: (err) => this.fail(err)
    });
  }

  confirmEmergencyStop(): void {
    this.pending.set('emergency-stop');
  }

  confirmReset(): void {
    this.pending.set('reset');
  }

  cancelConfirm(): void {
    this.pending.set(null);
  }

  runConfirmedAction(): void {
    const action = this.pending();
    this.pending.set(null);
    this.busy.set(true);

    if (action === 'emergency-stop') {
      this.api.emergencyStop().subscribe({
        next: (result) => {
          this.running.set(result.status.running);
          this.busy.set(false);
          this.announce(
            `Emergency stop: flattened ${result.closed_orders.length} position(s).`
          );
        },
        error: (err) => this.fail(err)
      });
    } else if (action === 'reset') {
      this.api.resetPortfolio().subscribe({
        next: () => {
          this.busy.set(false);
          this.announce('Paper portfolio reset.');
        },
        error: (err) => this.fail(err)
      });
    }
  }

  private announce(message: string): void {
    this.lastError.set(null);
    this.lastMessage.set(message);
    setTimeout(() => this.lastMessage.set(null), 4000);
  }

  private fail(err: unknown): void {
    this.busy.set(false);
    this.lastMessage.set(null);
    this.lastError.set(err instanceof Error ? err.message : 'Request failed.');
    setTimeout(() => this.lastError.set(null), 6000);
  }
}
