import { CommonModule } from '@angular/common';
import { Component, Input } from '@angular/core';

import { BacktestResult } from '../../core/models';

@Component({
  selector: 'app-backtest-result',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './backtest-result.component.html',
  styleUrl: './backtest-result.component.scss'
})
export class BacktestResultComponent {
  @Input({ required: true }) result!: BacktestResult;

  // A `EquityPoint[]` polyline plotted as an inline SVG path -- no
  // charting dependency added for one sparkline.
  get equityPath(): string {
    const points = this.result.equity_curve;
    if (points.length === 0) return '';

    const equities = points.map((p) => p.equity);
    const min = Math.min(...equities);
    const max = Math.max(...equities);
    const range = max - min || 1;

    const width = 600;
    const height = 120;
    const step = points.length > 1 ? width / (points.length - 1) : 0;

    return points
      .map((p, i) => {
        const x = i * step;
        const y = height - ((p.equity - min) / range) * height;
        return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(' ');
  }
}
