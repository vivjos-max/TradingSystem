import type {
  AccountOrdersResponse,
  AccountSummaryResponse,
  ApiResult,
  NiftyCandlesResponse,
  NiftyOptionChainResponse,
  NiftyQuoteResponse,
  SafeUnavailableResponse,
  SystemStatusResponse
} from "@/lib/types";

const apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

function isUnavailable(value: unknown): value is SafeUnavailableResponse {
  return typeof value === "object" && value !== null && "code" in value && (value as { code?: unknown }).code === "LIVE_STATE_UNAVAILABLE";
}

async function read<T>(path: string, signal?: AbortSignal): Promise<ApiResult<T>> {
  try {
    const response = await fetch(`${apiBaseUrl}${path}`, { method: "GET", signal });
    const payload: unknown = await response.json();
    if (response.status === 503 && isUnavailable(payload)) {
      return { data: null, unavailable: payload, error: null };
    }
    if (!response.ok) return { data: null, unavailable: null, error: `HTTP ${response.status}` };
    return { data: payload as T, unavailable: null, error: null };
  } catch {
    return { data: null, unavailable: null, error: "Network request failed" };
  }
}

export const api = {
  systemStatus: (signal?: AbortSignal) => read<SystemStatusResponse>("/api/v1/system/status", signal),
  niftyQuote: (signal?: AbortSignal) => read<NiftyQuoteResponse>("/api/v1/market/nifty", signal),
  niftyOptions: (signal?: AbortSignal) => read<NiftyOptionChainResponse>("/api/v1/market/nifty/options", signal),
  niftyCandles: (signal?: AbortSignal) => read<NiftyCandlesResponse>("/api/v1/market/nifty/candles?interval=5m", signal),
  accountSummary: (signal?: AbortSignal) => read<AccountSummaryResponse>("/api/v1/account/summary", signal),
  accountOrders: (signal?: AbortSignal) => read<AccountOrdersResponse>("/api/v1/account/orders", signal)
};
