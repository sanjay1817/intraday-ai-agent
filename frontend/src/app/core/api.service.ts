import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';

import {
  AggregateBacktestResult,
  AutoTradingStatus,
  BacktestRequest,
  BacktestResult,
  BacktestTradeRecord,
  ClosedTrade,
  EmergencyStopResponse,
  EquityPoint,
  Exchange,
  HealthResponse,
  HistoricalInterval,
  InstructorRecommendation,
  LogEntry,
  OptionAutoTradingStatus,
  OptionRecommendation,
  OptionRiskStatus,
  OptionsBacktestRequest,
  OptionTradeHistoryEntry,
  PaperOrder,
  PaperPosition,
  Portfolio,
  SignalLogEntry
} from './models';

// The Angular dev server runs on a different origin than the FastAPI
// backend (see `app.main.create_app`'s CORS middleware, added
// specifically for this) -- `localhost:8000` is `uvicorn`'s default.
const API_ROOT = 'http://localhost:8000';
const API_BASE = `/api/v1`;

export interface PlaceOrderRequest {
  order: {
    symbol: string;
    exchange: Exchange;
    side: 'BUY' | 'SELL';
    order_type: string;
    quantity: number;
    price?: number | null;
    trigger_price?: number | null;
    stop_loss_price?: number | null;
    target_price?: number | null;
    tag?: string | null;
  };
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  constructor(private readonly http: HttpClient) {}

  // -- paper trading --------------------------------------------------------------------

  getPortfolio(): Observable<Portfolio> {
    return this.http.get<Portfolio>(`${API_BASE}/paper/portfolio`);
  }

  getPositions(): Observable<PaperPosition[]> {
    return this.http.get<PaperPosition[]>(`${API_BASE}/paper/positions`);
  }

  getOrders(): Observable<PaperOrder[]> {
    return this.http.get<PaperOrder[]>(`${API_BASE}/paper/orders`);
  }

  getTrades(): Observable<ClosedTrade[]> {
    return this.http.get<ClosedTrade[]>(`${API_BASE}/paper/trades`);
  }

  placeOrder(body: PlaceOrderRequest): Observable<PaperOrder> {
    return this.http.post<PaperOrder>(`${API_BASE}/paper/order`, body);
  }

  resetPortfolio(initialCapital?: number): Observable<Portfolio> {
    return this.http.post<Portfolio>(`${API_BASE}/paper/reset`, {
      initial_capital: initialCapital ?? null
    });
  }

  // -- auto trading -----------------------------------------------------------------------

  getAutoStatus(): Observable<AutoTradingStatus> {
    return this.http.get<AutoTradingStatus>(`${API_BASE}/auto/status`);
  }

  startAutoTrading(): Observable<AutoTradingStatus> {
    return this.http.post<AutoTradingStatus>(`${API_BASE}/auto/start`, {});
  }

  stopAutoTrading(): Observable<AutoTradingStatus> {
    return this.http.post<AutoTradingStatus>(`${API_BASE}/auto/stop`, {});
  }

  emergencyStop(): Observable<EmergencyStopResponse> {
    return this.http.post<EmergencyStopResponse>(`${API_BASE}/auto/emergency-stop`, {});
  }

  // -- signals ----------------------------------------------------------------------------

  getSignal(
    exchange: Exchange,
    tradingsymbol: string,
    interval: HistoricalInterval = '5minute'
  ): Observable<InstructorRecommendation> {
    const params = new HttpParams()
      .set('exchange', exchange)
      .set('tradingsymbol', tradingsymbol)
      .set('interval', interval);
    return this.http.get<InstructorRecommendation>(`${API_BASE}/signals`, { params });
  }

  // -- logs -------------------------------------------------------------------------------

  getLogs(limit = 200): Observable<LogEntry[]> {
    const params = new HttpParams().set('limit', limit);
    return this.http.get<LogEntry[]>(`${API_BASE}/logs`, { params });
  }

  // -- health / options (Phase 4) ----------------------------------------------------------
  // Display-only: no order-placement/exit methods live here on purpose --
  // this phase's Options Dashboard reads state, it never places or
  // exits a trade.

  getHealth(): Observable<HealthResponse> {
    // `/health` is mounted at the API root (no `/api/v1` prefix -- see
    // `app.api.v1.routers.health`'s bare `APIRouter(tags=["health"])`
    // and `app.main.create_app`'s `app.include_router(health_router)`),
    // unlike every other endpoint this service calls.
    return this.http.get<HealthResponse>(`${API_ROOT}/health`);
  }

  getOptionRecommendation(
    underlying: string,
    timeframe: HistoricalInterval = '5minute'
  ): Observable<OptionRecommendation> {
    const params = new HttpParams().set('underlying', underlying).set('timeframe', timeframe);
    return this.http.get<OptionRecommendation>(`${API_BASE}/options/recommendation`, { params });
  }

  getOptionTrades(): Observable<OptionTradeHistoryEntry[]> {
    return this.http.get<OptionTradeHistoryEntry[]>(`${API_BASE}/options/paper/trades`);
  }

  getOptionRiskStatus(): Observable<OptionRiskStatus> {
    return this.http.get<OptionRiskStatus>(`${API_BASE}/options/risk/status`);
  }

  // -- auto options trading (Phase 5) ------------------------------------------------------
  // Display-only here too: no start/stop methods -- the Options Dashboard's
  // new section is read-only, matching Phase 4's own precedent for this page.

  getOptionAutoStatus(): Observable<OptionAutoTradingStatus> {
    return this.http.get<OptionAutoTradingStatus>(`${API_BASE}/options/auto/status`);
  }

  // -- historical backtesting --------------------------------------------------------------
  // Every response here is a HISTORICAL BACKTEST -- see
  // `app.backtest.dto.BACKTEST_DISCLAIMER` -- never live trading, never
  // manual/automatic paper trading, and no real order is ever placed.

  runBacktest(body: BacktestRequest): Observable<BacktestResult> {
    return this.http.post<BacktestResult>(`${API_BASE}/backtest/run`, body);
  }

  runOptionsBacktest(body: OptionsBacktestRequest): Observable<BacktestResult> {
    return this.http.post<BacktestResult>(`${API_BASE}/backtest/run-options`, body);
  }

  runBacktestBatch(bodies: BacktestRequest[]): Observable<AggregateBacktestResult> {
    return this.http.post<AggregateBacktestResult>(`${API_BASE}/backtest/run-batch`, bodies);
  }

  getBacktestRuns(): Observable<string[]> {
    return this.http.get<string[]>(`${API_BASE}/backtest/runs`);
  }

  getBacktestRun(runId: string): Observable<BacktestResult> {
    return this.http.get<BacktestResult>(`${API_BASE}/backtest/runs/${runId}`);
  }

  getBacktestTrades(runId: string): Observable<BacktestTradeRecord[]> {
    return this.http.get<BacktestTradeRecord[]>(`${API_BASE}/backtest/runs/${runId}/trades`);
  }

  getBacktestEquityCurve(runId: string): Observable<EquityPoint[]> {
    return this.http.get<EquityPoint[]>(`${API_BASE}/backtest/runs/${runId}/equity-curve`);
  }

  getBacktestSignals(runId: string): Observable<SignalLogEntry[]> {
    return this.http.get<SignalLogEntry[]>(`${API_BASE}/backtest/runs/${runId}/signals`);
  }
}
