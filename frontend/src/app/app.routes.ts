import { Routes } from '@angular/router';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'overview' },
  {
    path: 'overview',
    loadComponent: () => import('./pages/overview/overview.component').then((m) => m.OverviewComponent)
  },
  {
    path: 'portfolio',
    loadComponent: () =>
      import('./pages/portfolio/portfolio.component').then((m) => m.PortfolioComponent)
  },
  {
    path: 'positions',
    loadComponent: () =>
      import('./pages/positions/positions.component').then((m) => m.PositionsComponent)
  },
  {
    path: 'orders',
    loadComponent: () => import('./pages/orders/orders.component').then((m) => m.OrdersComponent)
  },
  {
    path: 'trade-history',
    loadComponent: () =>
      import('./pages/trade-history/trade-history.component').then((m) => m.TradeHistoryComponent)
  },
  {
    path: 'ai-signals',
    loadComponent: () =>
      import('./pages/ai-signals/ai-signals.component').then((m) => m.AiSignalsComponent)
  },
  {
    path: 'auto-status',
    loadComponent: () =>
      import('./pages/auto-status/auto-status.component').then((m) => m.AutoStatusComponent)
  },
  {
    path: 'logs',
    loadComponent: () => import('./pages/logs/logs.component').then((m) => m.LogsComponent)
  },
  {
    path: 'options',
    loadComponent: () =>
      import('./pages/options-dashboard/options-dashboard.component').then(
        (m) => m.OptionsDashboardComponent
      )
  },
  { path: '**', redirectTo: 'overview' }
];
