import { CommonModule } from '@angular/common';
import { Component, Input, computed, signal } from '@angular/core';

export interface DonutSlice {
  label: string;
  value: number;
  color: string;
}

interface RenderedSlice extends DonutSlice {
  path: string;
  percent: number;
}

const RADIUS = 45;
const CENTER = 50;

@Component({
  selector: 'app-donut-chart',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './donut-chart.component.html',
  styleUrl: './donut-chart.component.scss'
})
export class DonutChartComponent {
  private readonly _slices = signal<DonutSlice[]>([]);

  @Input() set slices(value: DonutSlice[]) {
    this._slices.set(value ?? []);
  }
  @Input() size = 140;

  readonly total = computed(() => this._slices().reduce((sum, s) => sum + s.value, 0));
  readonly isEmpty = computed(() => this.total() === 0);

  readonly rendered = computed<RenderedSlice[]>(() => {
    const total = this.total();
    if (total === 0) return [];

    let cumulativeAngle = -Math.PI / 2;
    return this._slices()
      .filter((s) => s.value > 0)
      .map((slice) => {
        const fraction = slice.value / total;
        const angle = fraction * 2 * Math.PI;
        const startAngle = cumulativeAngle;
        const endAngle = cumulativeAngle + angle;
        cumulativeAngle = endAngle;

        const x1 = CENTER + RADIUS * Math.cos(startAngle);
        const y1 = CENTER + RADIUS * Math.sin(startAngle);
        const x2 = CENTER + RADIUS * Math.cos(endAngle);
        const y2 = CENTER + RADIUS * Math.sin(endAngle);
        const largeArc = angle > Math.PI ? 1 : 0;

        const path =
          fraction >= 0.999
            ? `M ${CENTER} ${CENTER - RADIUS} A ${RADIUS} ${RADIUS} 0 1 1 ${CENTER - 0.01} ${CENTER - RADIUS} Z`
            : `M ${CENTER} ${CENTER} L ${x1.toFixed(2)} ${y1.toFixed(2)} A ${RADIUS} ${RADIUS} 0 ${largeArc} 1 ${x2.toFixed(2)} ${y2.toFixed(2)} Z`;

        return { ...slice, path, percent: Math.round(fraction * 100) };
      });
  });
}
