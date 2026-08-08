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

// -- Historical Backtesting (mirrors `app/backtest/dto.py`) ---------------------------------

export interface TransactionCostModel {
  brokerage_percent: number;
  brokerage_max_per_order: number;
  stt_percent: number;
  exchange_txn_charge_percent: number;
  sebi_charges_percent: number;
  stamp_duty_percent: number;
  gst_percent: number;
  slippage_percent: number;
}

export interface BacktestRequest {
  symbol: string;
  exchange: Exchange;
  historical_date: string; // YYYY-MM-DD
  start_time?: string; // HH:MM:SS
  end_time?: string; // HH:MM:SS
  interval?: HistoricalInterval;
  initial_capital: number;
  confidence_threshold?: number;
  capital_fraction_per_trade?: number;
  cost_model?: Partial<TransactionCostModel>;
}

export interface OptionsBacktestRequest extends BacktestRequest {
  strike_mode?: 'ATM' | 'ITM' | 'OTM';
  expiry_mode?: 'NEAREST_WEEKLY' | 'NEXT_WEEKLY' | 'MONTHLY';
}

export interface BacktestTradeRecord {
  trade_id: string;
  symbol: string;
  exchange: Exchange;
  side: OrderSide;
  quantity: number;
  entry_time: string;
  entry_signal_price: number;
  entry_fill_price: number;
  exit_time: string;
  exit_signal_price: number;
  exit_fill_price: number;
  stop_loss: number | null;
  target: number | null;
  exit_reason: string;
  gross_pnl: number;
  charges: number;
  slippage_cost: number;
  net_pnl: number;
  confidence: number | null;
  strategy_signal: string;
}

export interface SignalLogEntry {
  timestamp: string;
  action: RecommendationAction;
  confidence: number;
  indicators: Record<string, number | null>;
  reasoning: string;
}

export interface EquityPoint {
  timestamp: string;
  equity: number;
  drawdown: number;
  drawdown_percent: number;
}

export interface BacktestSummary {
  initial_capital: number;
  final_capital: number;
  total_pnl: number;
  total_pnl_percent: number;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  average_profit: number;
  average_loss: number;
  largest_win: number;
  largest_loss: number;
  max_drawdown: number;
  max_drawdown_percent: number;
  profit_factor: number | null;
}

export interface BacktestResult {
  run_id: string;
  request: BacktestRequest;
  summary: BacktestSummary;
  trades: BacktestTradeRecord[];
  equity_curve: EquityPoint[];
  signal_log: SignalLogEntry[];
  warnings: string[];
  disclaimer: string;
  generated_at: string;
}

export interface AggregateBacktestSummary {
  total_sessions: number;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  total_pnl: number;
  win_rate: number;
  max_drawdown: number;
}

export interface AggregateBacktestResult {
  run_id: string;
  session_results: BacktestResult[];
  aggregate: AggregateBacktestSummary;
  disclaimer: string;
  generated_at: string;
}
