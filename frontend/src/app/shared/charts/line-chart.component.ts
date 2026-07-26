import { CommonModule } from '@angular/common';
import { Component, Input, computed, signal } from '@angular/core';

export interface LinePoint {
  label: string;
  value: number;
}

// A single hand-rolled SVG polyline -- no charting library, per Phase
// 4's "lightweight dashboard" requirement. Used for the equity curve.
@Component({
  selector: 'app-line-chart',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './line-chart.component.html',
  styleUrl: './chart-common.scss'
})
export class LineChartComponent {
  private readonly _points = signal<LinePoint[]>([]);

  @Input() set points(value: LinePoint[]) {
    this._points.set(value ?? []);
  }
  @Input() strokeColor = 'var(--accent)';
  @Input() height = 160;

  private readonly width = 600;

  readonly polyline = computed(() => {
    const pts = this._points();
    if (pts.length === 0) return '';
    const values = pts.map((p) => p.value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    const stepX = pts.length > 1 ? this.width / (pts.length - 1) : 0;
    return pts
      .map((p, i) => {
        const x = pts.length > 1 ? i * stepX : this.width / 2;
        const y = this.height - ((p.value - min) / range) * (this.height - 10) - 5;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(' ');
  });

  readonly isEmpty = computed(() => this._points().length === 0);
  readonly latest = computed(() => {
    const pts = this._points();
    return pts.length ? pts[pts.length - 1].value : null;
  });

  readonly viewBox = computed(() => `0 0 ${this.width} ${this.height}`);
}
