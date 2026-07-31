import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { OptionsDashboardComponent } from './options-dashboard.component';
import {
  HealthResponse,
  OptionRecommendation,
  OptionTradeHistoryEntry,
  PaperPosition,
  Portfolio
} from '../../core/models';

const HEALTH: HealthResponse = {
  status: 'ok',
  app_name: 'Test Trading Agent',
  app_env: 'testing',
  uptime_seconds: 12.3,
  ready: true,
  trading_mode: 'OPTIONS',
  default_broker: 'angel_one',
  options_infrastructure_available: true,
  option_underlyings: ['NIFTY', 'BANKNIFTY'],
  option_default_lot_size: 50
};

const RECOMMENDATION: OptionRecommendation = {
  underlying: 'NIFTY',
  signal: 'BULLISH',
  tradingsymbol: 'NIFTY07AUG202624000CE',
  expiry: '2026-08-07',
  strike: 24000,
  option_type: 'CE',
  premium: 100.5,
  underlying_ltp: 24000,
  confidence: 80,
  reasoning: 'strong signal',
  generated_at: '2026-07-28T10:00:00Z'
};

const PORTFOLIO: Portfolio = {
  cash: 900000,
  available_cash: 850000,
  used_capital: 100000,
  initial_capital: 1000000,
  realized_pnl: 5000,
  unrealized_pnl: 2000,
  total_pnl: 7000,
  equity: 1007000,
  positions: [],
  updated_at: '2026-07-28T10:00:00Z'
};

const NFO_POSITION: PaperPosition = {
  symbol: 'NIFTY07AUG202624000CE',
  exchange: 'NFO',
  quantity: 50,
  average_price: 100,
  last_price: 105,
  realized_pnl: 0,
  unrealized_pnl: 250,
  opened_at: '2026-07-28T10:00:00Z',
  updated_at: '2026-07-28T10:00:00Z'
};

const AUTO_STATUS = {
  running: true,
  started_at: '2026-07-28T09:15:00Z',
  underlyings: ['NIFTY'],
  cycle_count: 12,
  last_cycle_at: '2026-07-28T10:00:00Z',
  last_scan_at: '2026-07-28T10:00:00Z',
  next_scan_at: '2026-07-28T10:00:30Z',
  last_action: 'trade_entered:NIFTY',
  open_position_count: 1,
  trades_today: 2,
  daily_realized_pnl: 750,
  last_error: null
};

const TRADE: OptionTradeHistoryEntry = {
  trade_id: 't1',
  underlying: 'NIFTY',
  tradingsymbol: 'NIFTY07AUG202624000CE',
  entry_premium: 100,
  exit_premium: 120,
  pnl: 1000,
  holding_seconds: 754,
  reason: 'MANUAL',
  confidence: 80,
  reasoning: 'strong',
  entry_timestamp: '2026-07-28T10:00:00Z',
  exit_timestamp: '2026-07-28T10:12:34Z'
};

describe('OptionsDashboardComponent', () => {
  let fixture: ComponentFixture<OptionsDashboardComponent>;
  let httpMock: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [OptionsDashboardComponent],
      providers: [provideHttpClient(), provideHttpClientTesting()]
    }).compileComponents();

    fixture = TestBed.createComponent(OptionsDashboardComponent);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  function flushAll(options: {
    recommendationError?: boolean;
    positions?: PaperPosition[];
    trades?: OptionTradeHistoryEntry[];
    riskError?: boolean;
    optionAutoError?: boolean;
  } = {}): void {
    fixture.detectChanges();

    httpMock.expectOne((r) => r.url.endsWith('/health')).flush(HEALTH);

    const recReq = httpMock.expectOne((r) => r.url.includes('/options/recommendation'));
    if (options.recommendationError) {
      recReq.flush({ error: { type: 'OptionsInfrastructureUnavailableError' } }, { status: 503, statusText: 'Service Unavailable' });
    } else {
      recReq.flush(RECOMMENDATION);
    }

    httpMock.expectOne((r) => r.url.includes('/paper/portfolio')).flush(PORTFOLIO);
    httpMock.expectOne((r) => r.url.includes('/paper/positions')).flush(options.positions ?? [NFO_POSITION]);
    httpMock
      .expectOne((r) => r.url.includes('/auto/status') && !r.url.includes('/options/'))
      .flush({
        running: false,
        started_at: null,
        symbols: [],
        cycle_count: 0,
        last_cycle_at: null,
        open_position_count: 0,
        trades_today: 0,
        daily_realized_pnl: 0,
        consecutive_losses: 0,
        cooldown_until: null,
        last_error: null
      });
    httpMock.expectOne((r) => r.url.includes('/options/paper/trades')).flush(options.trades ?? [TRADE]);

    const riskReq = httpMock.expectOne((r) => r.url.includes('/options/risk/status'));
    if (options.riskError) {
      riskReq.flush({ error: { type: 'OptionsInfrastructureUnavailableError' } }, { status: 503, statusText: 'Service Unavailable' });
    } else {
      riskReq.flush({
        current_premium_exposure: 5000,
        max_premium_exposure: 200000,
        remaining_premium_capacity: 195000,
        daily_realized_pnl: 500,
        max_daily_loss: 10000,
        max_lots_per_order: 10,
        max_premium_per_order: 50000
      });
    }

    const autoReq = httpMock.expectOne((r) => r.url.includes('/options/auto/status'));
    if (options.optionAutoError) {
      autoReq.flush(
        { error: { type: 'OptionsInfrastructureUnavailableError' } },
        { status: 503, statusText: 'Service Unavailable' }
      );
    } else {
      autoReq.flush(AUTO_STATUS);
    }

    fixture.detectChanges();
  }

  it('creates without error', () => {
    flushAll();
    expect(fixture.componentInstance).toBeTruthy();
  });

  it('renders an unavailable state when the recommendation call errors', () => {
    flushAll({ recommendationError: true });
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Recommendation unavailable');
  });

  it('renders positions and trades tables when data is present', () => {
    flushAll();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('NIFTY07AUG202624000CE');
    expect(el.textContent).toContain('OPEN');
  });

  it('renders empty states when arrays are empty', () => {
    flushAll({ positions: [], trades: [] });
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('No open option positions');
    expect(el.textContent).toContain('No option trades yet');
  });

  it('renders risk unavailable state when the risk status call errors', () => {
    flushAll({ riskError: true });
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Risk status unavailable');
  });

  it('renders the auto options trading section when populated', () => {
    flushAll();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Auto Options Trading');
    expect(el.textContent).toContain('RUNNING');
    expect(el.textContent).toContain('trade_entered:NIFTY');
  });

  it('renders an unavailable state when the auto options status call errors', () => {
    flushAll({ optionAutoError: true });
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Auto options trading status unavailable');
  });
});
