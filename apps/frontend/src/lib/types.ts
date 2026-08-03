export type SourceTimestamp = string;

export interface SafeUnavailableResponse {
  code: "LIVE_STATE_UNAVAILABLE";
  correlation_id: string;
  occurred_at: SourceTimestamp;
}

export interface ServiceResponse {
  service: string;
  state: string;
  source_observed_at: SourceTimestamp;
}

export interface SystemStatusResponse {
  state: string;
  source_observed_at: SourceTimestamp;
  services: ServiceResponse[];
  served_at: SourceTimestamp;
  correlation_id: string;
}

export interface QuoteSnapshotResponse {
  instrument_key: string;
  last_price: number;
  source: string;
  source_observed_at: SourceTimestamp;
}

export interface NiftyQuoteResponse {
  spot: QuoteSnapshotResponse;
  nearest_future: QuoteSnapshotResponse;
  served_at: SourceTimestamp;
  correlation_id: string;
}

export interface OptionContractResponse {
  instrument_token: string;
  strike: number;
  option_type: string;
  ltp: number | null;
  bid: number | null;
  ask: number | null;
  spread: number | null;
  volume: number | null;
  open_interest: number | null;
  implied_volatility: number | null;
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
  rho: number | null;
}

export interface NiftyOptionChainResponse {
  expiry: string;
  source: string;
  source_observed_at: SourceTimestamp;
  atm_strike: number;
  selected_strikes: number[];
  contracts: OptionContractResponse[];
  served_at: SourceTimestamp;
  correlation_id: string;
}

export interface CandleResponse {
  instrument_key: string;
  interval: string;
  source_opened_at: SourceTimestamp;
  source_closed_at: SourceTimestamp;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
  finalized: boolean;
  source: string;
}

export interface NiftyCandlesResponse {
  interval: string;
  candles: CandleResponse[];
  served_at: SourceTimestamp;
  correlation_id: string;
}

export interface PositionResponse {
  instrument_token: string;
  quantity: number;
  average_price: number;
  last_price: number | null;
  realized_pnl: number;
  unrealized_pnl: number | null;
  source: string;
  source_observed_at: SourceTimestamp;
}

export interface AccountSummaryResponse {
  available_cash: number;
  used_margin: number;
  collateral: number;
  source: string;
  source_observed_at: SourceTimestamp;
  positions: PositionResponse[];
  served_at: SourceTimestamp;
  correlation_id: string;
}

export interface FillResponse {
  fill_id: string;
  order_id: string;
  instrument_token: string;
  quantity: number;
  price: number;
  source_filled_at: SourceTimestamp;
  source: string;
  source_observed_at: SourceTimestamp;
}

export interface OrderResponse {
  order_id: string;
  instrument_token: string;
  side: string;
  quantity: number;
  status: string;
  source_created_at: SourceTimestamp;
  source_updated_at: SourceTimestamp;
  source: string;
  source_observed_at: SourceTimestamp;
  reconciliation: "matched" | "unmatched";
  fills: FillResponse[];
}

export interface AccountOrdersResponse {
  orders: OrderResponse[];
  unmatched_fills: FillResponse[];
  orders_source: string;
  orders_source_observed_at: SourceTimestamp;
  fills_source: string;
  fills_source_observed_at: SourceTimestamp;
  served_at: SourceTimestamp;
  correlation_id: string;
}

export type ApiResult<T> =
  | { data: T; unavailable: null; error: null }
  | { data: null; unavailable: SafeUnavailableResponse; error: null }
  | { data: null; unavailable: null; error: string };
