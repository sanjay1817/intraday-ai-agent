import { CommonModule } from '@angular/common';
import { Component, signal } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

import { ThemeService } from './core/theme.service';
import { ControlsBarComponent } from './shared/controls-bar/controls-bar.component';

interface NavLink {
  path: string;
  label: string;
}

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, RouterLink, RouterLinkActive, RouterOutlet, ControlsBarComponent],
  templateUrl: './app.component.html',
  styleUrl: './app.component.scss'
})
export class AppComponent {
  readonly navLinks: NavLink[] = [
    { path: '/overview', label: 'Overview' },
    { path: '/portfolio', label: 'Portfolio' },
    { path: '/positions', label: 'Open Positions' },
    { path: '/orders', label: 'Orders' },
    { path: '/trade-history', label: 'Trade History' },
    { path: '/ai-signals', label: 'AI Signals' },
    { path: '/auto-status', label: 'Auto Trading' },
    { path: '/logs', label: 'Logs' }
  ];

  readonly menuOpen = signal(false);

  constructor(readonly theme: ThemeService) {}

  toggleMenu(): void {
    this.menuOpen.update((open) => !open);
  }

  closeMenu(): void {
    this.menuOpen.set(false);
  }
}
