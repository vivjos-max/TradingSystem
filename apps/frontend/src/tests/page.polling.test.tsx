import "@testing-library/jest-dom/vitest";
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import Page from "@/app/page";

const liveSystem = { state: "live", source_observed_at: "2026-08-03T04:30:00Z", services: [], served_at: "2026-08-03T04:30:01Z", correlation_id: "system-correlation" };
const quote = { spot: { instrument_key: "NSE:NIFTY50", last_price: 24812.35, source: "groww", source_observed_at: "2026-08-03T04:30:00Z" }, nearest_future: { instrument_key: "NFO:NIFTY-FUT", last_price: 24840.5, source: "groww", source_observed_at: "2026-08-03T04:30:00Z" }, served_at: "2026-08-03T04:30:01Z", correlation_id: "quote-correlation" };
const optionsUnavailable = { code: "LIVE_STATE_UNAVAILABLE", correlation_id: "options-unavailable", occurred_at: "2026-08-03T04:30:02Z" };
const candles = { interval: "5m", candles: [{ instrument_key: "NSE:NIFTY50", interval: "5m", source_opened_at: "2026-08-03T04:25:00Z", source_closed_at: "2026-08-03T04:30:00Z", open: 24800, high: 24830, low: 24790, close: 24812.35, volume: 1200, finalized: true, source: "groww" }], served_at: "2026-08-03T04:30:01Z", correlation_id: "candle-correlation" };
const account = { available_cash: 100000, used_margin: 25000, collateral: 5000, source: "groww", source_observed_at: "2026-08-03T04:30:00Z", positions: [], served_at: "2026-08-03T04:30:01Z", correlation_id: "account-correlation" };
const orders = { orders: [], unmatched_fills: [], orders_source: "groww", orders_source_observed_at: "2026-08-03T04:30:00Z", fills_source: "groww", fills_source_observed_at: "2026-08-03T04:30:00Z", served_at: "2026-08-03T04:30:01Z", correlation_id: "orders-correlation" };

function responseFor(input: string) {
  if (input.includes("/system/status")) return new Response(JSON.stringify(liveSystem));
  if (input.includes("/market/nifty/options")) return new Response(JSON.stringify(optionsUnavailable), { status: 503 });
  if (input.includes("/market/nifty/candles")) return new Response(JSON.stringify(candles));
  if (input.includes("/market/nifty")) return new Response(JSON.stringify(quote));
  if (input.includes("/account/summary")) return new Response(JSON.stringify(account));
  return new Response(JSON.stringify(orders));
}

describe("dashboard polling", () => {
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

  it("waits for a genuine source timestamp instead of fabricating an epoch value", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
    render(<Page />);

    expect(screen.getByText("Awaiting source timestamp")).toBeVisible();
  });

  it("keeps an unavailable market group blocked after a successful system refresh", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn((input: string) => Promise.resolve(responseFor(input))));
    render(<Page />);

    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByRole("status")).toHaveTextContent("BLOCKED");

    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(screen.getByRole("status")).toHaveTextContent("BLOCKED");
    expect(screen.getByText("LIVE_STATE_UNAVAILABLE")).toBeVisible();
  });

  it("does not overlap slow system refreshes and aborts the outstanding request on cleanup", async () => {
    vi.useFakeTimers();
    let systemSignal: AbortSignal | undefined;
    vi.stubGlobal("fetch", vi.fn((input: string, init?: RequestInit) => {
      if (input.includes("/system/status")) {
        systemSignal = init?.signal ?? undefined;
        return new Promise<Response>(() => undefined);
      }
      return Promise.resolve(responseFor(input));
    }));
    const view = render(<Page />);

    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    const systemRequests = vi.mocked(fetch).mock.calls.filter(([input]) => String(input).includes("/system/status"));
    expect(systemRequests).toHaveLength(1);

    view.unmount();
    expect(systemSignal?.aborted).toBe(true);
  });
});
