import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';

import {
  AutoTradingStatus,
  ClosedTrade,
  EmergencyStopResponse,
  Exchange,
  HistoricalInterval,
  InstructorRecommendation,
  LogEntry,
  PaperOrder,
  PaperPosition,
  Portfolio
} from './models';

// The Angular dev server runs on a different origin than the FastAPI
// backend (see `app.main.create_app`'s CORS middleware, added
// specifically for this) -- `localhost:8000` is `uvicorn`'s default.
const API_BASE = 'http://localhost:8000/api/v1';

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
}
