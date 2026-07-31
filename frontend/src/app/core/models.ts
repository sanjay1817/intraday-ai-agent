// TypeScript mirrors of the FastAPI backend's Pydantic response models.
// Field names/shapes are kept identical to `app/paper/models.py`,
// `app/auto/models.py`, `app/instructor/recommendation.py`, and
// `app/core/logging.py` so the two stay trivially diffable.

export type Exchange = 'NSE' | 'BSE' | 'NFO' | 'BFO' | 'MCX' | 'CDS';
export type OrderSide = 'BUY' | 'SELL';
export type OrderStatus =
  | 'PENDING'
  | 'OPEN'
  | 'COMPLETE'
  | 'CANCELLED'
  | 'REJECTED'
  | 'TRIGGER_PENDING'
  | 'UNKNOWN';
export type OrderValidity = 'DAY' | 'IOC' | 'GTC';
export type PaperOrderType = 'MARKET' | 'LIMIT' | 'STOP' | 'STOP_LIMIT' | 'TRAILING_STOP';
export type RecommendationAction = 'BUY' | 'SELL' | 'HOLD';
export type HistoricalInterval =
  | '1minute'
  | '3minute'
  | '5minute'
  | '10minute'
  | '15minute'
  | '30minute'
  | '60minute'
  | 'day';

export interface TradeMetadata {
  confidence: number | null;
  agreeing_strategies: string[];
  conflicting_strategies: string[];
  indicators_used: string[];
  reasoning: string | null;
}

export interface PaperOrder {
  order_id: string;
  symbol: string;
  exchange: Exchange;
  side: OrderSide;
  order_type: PaperOrderType;
  quantity: number;
  filled_quantity: number;
  price: number | null;
  trigger_price: number | null;
  trailing_amount: number | null;
  trailing_percent: number | null;
  average_fill_price: number | null;
  status: OrderStatus;
  validity: OrderValidity;
  expires_at: string | null;
  stop_loss_price: number | null;
  target_price: number | null;
  parent_order_id: string | null;
  oco_group_id: string | null;
  tag: string | null;
  metadata: TradeMetadata | null;
  status_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface PaperPosition {
  symbol: string;
  exchange: Exchange;
  quantity: number;
  average_price: number;
  last_price: number;
  realized_pnl: number;
  unrealized_pnl: number;
  opened_at: string;
  updated_at: string;
}

export interface ClosedTrade {
  trade_id: string;
  symbol: string;
  exchange: Exchange;
  side: OrderSide;
  quantity: number;
  entry_price: number;
  exit_price: number;
  stop_loss_price: number | null;
  target_price: number | null;
  pnl: number;
  entry_order_id: string;
  exit_order_id: string;
  entry_timestamp: string;
  exit_timestamp: string;
  metadata: TradeMetadata | null;
}

export interface Portfolio {
  cash: number;
  available_cash: number;
  used_capital: number;
  initial_capital: number;
  realized_pnl: number;
  unrealized_pnl: number;
  total_pnl: number;
  equity: number;
  positions: PaperPosition[];
  updated_at: string;
}

export interface AutoTradingStatus {
  running: boolean;
  started_at: string | null;
  symbols: string[];
  cycle_count: number;
  last_cycle_at: string | null;
  open_position_count: number;
  trades_today: number;
  daily_realized_pnl: number;
  consecutive_losses: number;
  cooldown_until: string | null;
  last_error: string | null;
}

export interface EmergencyStopResponse {
  status: AutoTradingStatus;
  closed_orders: PaperOrder[];
}

export interface InstructorRecommendation {
  symbol: string;
  exchange: Exchange;
  timeframe: HistoricalInterval;
  action: RecommendationAction;
  confidence: number;
  entry: number | null;
  stop_loss: number | null;
  targets: number[];
  risk_reward: number | null;
  confirmation_count: number;
  total_strategy_count: number;
  reasoning: string;
  warnings: string[];
  generated_at: string;
}

export interface LogEntry {
  timestamp: string;
  level: string;
  logger: string;
  message: string;
}

// -- options (Phase 1-4) -----------------------------------------------------------------
// Mirrors `app/options/schemas.py`'s `OptionRecommendation`/
// `OptionTradeHistoryEntry`/`OptionRiskStatus`, and `/health`'s Phase 4
// additive fields (`app/api/v1/routers/health.py`).

export type OptionSignal = 'BULLISH' | 'BEARISH' | 'NO_TRADE';
export type OptionType = 'CE' | 'PE';

export interface OptionRecommendation {
  underlying: string;
  signal: OptionSignal;
  tradingsymbol: string | null;
  expiry: string | null;
  strike: number | null;
  option_type: OptionType | null;
  premium: number | null;
  underlying_ltp: number;
  confidence: number;
  reasoning: string;
  generated_at: string;
}

export interface OptionTradeHistoryEntry {
  trade_id: string;
  underlying: string;
  tradingsymbol: string;
  entry_premium: number;
  exit_premium: number;
  pnl: number;
  holding_seconds: number;
  reason: string | null;
  confidence: number | null;
  reasoning: string | null;
  entry_timestamp: string;
  exit_timestamp: string;
}

export interface OptionRiskStatus {
  current_premium_exposure: number;
  max_premium_exposure: number;
  remaining_premium_capacity: number;
  daily_realized_pnl: number;
  max_daily_loss: number;
  max_lots_per_order: number;
  max_premium_per_order: number;
}

// -- auto options trading (Phase 5) -------------------------------------------------------
// Mirrors `app/options/auto_trading.py`'s `AutoOptionsStatus`.

export interface OptionAutoTradingStatus {
  running: boolean;
  started_at: string | null;
  underlyings: string[];
  cycle_count: number;
  last_cycle_at: string | null;
  last_scan_at: string | null;
  next_scan_at: string | null;
  last_action: string;
  open_position_count: number;
  trades_today: number;
  daily_realized_pnl: number;
  last_error: string | null;
}

export interface HealthResponse {
  status: string;
  app_name: string;
  app_env: string;
  uptime_seconds: number;
  ready: boolean;
  trading_mode: string;
  default_broker: string;
  options_infrastructure_available: boolean;
  option_underlyings: string[];
  option_default_lot_size: number;
}
