import "@testing-library/jest-dom/vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Page from "@/app/page";

const systemStatus = {
  state: "blocked",
  source_observed_at: "2026-08-03T04:30:00Z",
  services: [
    { service: "groww", state: "unavailable", source_observed_at: "2026-08-03T04:30:00Z" },
    { service: "repository", state: "healthy", source_observed_at: "2026-08-03T04:29:59Z" }
  ],
  served_at: "2026-08-03T04:30:01Z",
  correlation_id: "system-correlation"
};

const quote = {
  spot: { instrument_key: "NSE:NIFTY50", last_price: 24812.35, source: "groww", source_observed_at: "2026-08-03T04:30:00Z" },
  nearest_future: { instrument_key: "NFO:NIFTY-FUT", last_price: 24840.5, source: "groww", source_observed_at: "2026-08-03T04:30:00Z" },
  served_at: "2026-08-03T04:30:01Z",
  correlation_id: "quote-correlation"
};

const candles = {
  interval: "5m",
  candles: [{ instrument_key: "NSE:NIFTY50", interval: "5m", source_opened_at: "2026-08-03T04:25:00Z", source_closed_at: "2026-08-03T04:30:00Z", open: 24800, high: 24830, low: 24790, close: 24812.35, volume: 1200, finalized: true, source: "groww" }],
  served_at: "2026-08-03T04:30:01Z",
  correlation_id: "candle-correlation"
};

const account = {
  available_cash: 100000, used_margin: 25000, collateral: 5000, source: "groww", source_observed_at: "2026-08-03T04:30:00Z",
  positions: [{ instrument_token: "NIFTY-CE-24800", quantity: 50, average_price: 120, last_price: null, realized_pnl: 0, unrealized_pnl: null, source: "groww", source_observed_at: "2026-08-03T04:30:00Z" }],
  served_at: "2026-08-03T04:30:01Z", correlation_id: "account-correlation"
};

const orders = {
  orders: [], unmatched_fills: [], orders_source: "groww", orders_source_observed_at: "2026-08-03T04:30:00Z", fills_source: "groww", fills_source_observed_at: "2026-08-03T04:30:00Z", served_at: "2026-08-03T04:30:01Z", correlation_id: "orders-correlation"
};

const optionChain = {
  expiry: "2026-08-06", source: "groww", source_observed_at: "2026-08-03T04:30:00Z", atm_strike: 24800, selected_strikes: [24800], contracts: [], served_at: "2026-08-03T04:30:01Z", correlation_id: "options-correlation"
};

describe("dashboard page", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn((input: string) => {
      const payload = input.includes("/system/status") ? systemStatus
        : input.includes("/market/nifty/options") ? optionChain
        : input.includes("/market/nifty/candles") ? candles
        : input.includes("/market/nifty") ? quote
        : input.includes("/account/summary") ? account
        : orders;
      return Promise.resolve(new Response(JSON.stringify(payload), { status: 200 }));
    }));
  });

  afterEach(() => vi.unstubAllGlobals());

  it("renders read-only operational data from complete API responses", async () => {
    const { container } = render(<Page />);

    expect(await screen.findByRole("status")).toHaveTextContent("BLOCKED");
    expect(screen.getAllByText("24,812.35")[0]).toBeVisible();
    expect(screen.getByText("1,00,000.00")).toBeVisible();
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(6));

    const interactive = container.querySelectorAll("button, input, select, textarea, a[href]");
    const forbidden = /approve|buy|sell|exit|execute|order/i;
    expect([...interactive].some((element) => forbidden.test(element.textContent ?? element.getAttribute("aria-label") ?? ""))).toBe(false);
  });

  it("makes a SafeUnavailableResponse an explicit blocked state", async () => {
    vi.stubGlobal("fetch", vi.fn((input: string) => {
      if (input.includes("/system/status")) {
        return Promise.resolve(new Response(JSON.stringify({ code: "LIVE_STATE_UNAVAILABLE", correlation_id: "unavailable-correlation", occurred_at: "2026-08-03T04:30:02Z" }), { status: 503 }));
      }
      const payload = input.includes("/market/nifty/options") ? optionChain
        : input.includes("/market/nifty/candles") ? candles
        : input.includes("/market/nifty") ? quote
        : input.includes("/account/summary") ? account
        : orders;
      return Promise.resolve(new Response(JSON.stringify(payload), { status: 200 }));
    }));

    render(<Page />);

    expect(await screen.findByRole("status")).toHaveTextContent("BLOCKED");
    expect(screen.getByText("LIVE_STATE_UNAVAILABLE")).toBeVisible();
  });

  it("contains no execution endpoint or mutation method in frontend source", async () => {
    const sourceFiles = import.meta.glob("../{app,components,lib}/**/*.{ts,tsx}", { eager: true, query: "?raw", import: "default" });
    const source = Object.values(sourceFiles).join("\n");

    expect(source).not.toMatch(/\/orders\/place|\/execute/i);
    expect(source).not.toMatch(/fetch\([^)]*,\s*\{\s*method:\s*["'](?:POST|PUT|PATCH|DELETE)["']/i);
  });
});
