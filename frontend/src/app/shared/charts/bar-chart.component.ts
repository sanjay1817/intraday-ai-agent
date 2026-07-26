import { CommonModule } from '@angular/common';
import { Component, Input, computed, signal } from '@angular/core';

export interface BarPoint {
  label: string;
  value: number;
}

interface RenderedBar extends BarPoint {
  x: number;
  y: number;
  width: number;
  height: number;
  color: string;
}

// Hand-rolled SVG bar chart -- positive/negative bars colored by sign,
// used for Daily P&L and Trade Distribution.
@Component({
  selector: 'app-bar-chart',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './bar-chart.component.html',
  styleUrl: './chart-common.scss'
})
export class BarChartComponent {
  private readonly _points = signal<BarPoint[]>([]);

  @Input() set points(value: BarPoint[]) {
    this._points.set(value ?? []);
  }
  @Input() height = 160;
  @Input() positiveColor = 'var(--green)';
  @Input() negativeColor = 'var(--red)';
  @Input() singleColor: string | null = null;

  private readonly width = 600;
  private readonly zeroLine = computed(() => this.height / 2);

  readonly isEmpty = computed(() => this._points().length === 0);
  readonly viewBox = computed(() => `0 0 ${this.width} ${this.height}`);

  readonly bars = computed<RenderedBar[]>(() => {
    const pts = this._points();
    if (pts.length === 0) return [];

    const usesZeroLine = this.singleColor === null;
    const values = pts.map((p) => p.value);
    const maxAbs = Math.max(...values.map((v) => Math.abs(v)), 1);
    const gap = 4;
    const barWidth = Math.max((this.width - gap * (pts.length + 1)) / pts.length, 2);

    return pts.map((p, i) => {
      const x = gap + i * (barWidth + gap);
      if (usesZeroLine) {
        const half = this.zeroLine();
        const scaled = (Math.abs(p.value) / maxAbs) * (half - 8);
        const y = p.value >= 0 ? half - scaled : half;
        return {
          ...p,
          x,
          y,
          width: barWidth,
          height: Math.max(scaled, 1),
          color: p.value >= 0 ? this.positiveColor : this.negativeColor
        };
      }
      const scaled = (Math.abs(p.value) / maxAbs) * (this.height - 12);
      return {
        ...p,
        x,
        y: this.height - scaled,
        width: barWidth,
        height: Math.max(scaled, 1),
        color: this.singleColor!
      };
    });
  });
}
