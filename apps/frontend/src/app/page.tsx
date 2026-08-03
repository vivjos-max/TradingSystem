"use client";

import { useCallback, useEffect, useState } from "react";
import { AccountPanel } from "@/components/AccountPanel";
import { CandlePanel } from "@/components/CandlePanel";
import { GlobalStatus } from "@/components/GlobalStatus";
import { MarketPanel } from "@/components/MarketPanel";
import { OptionTable } from "@/components/OptionTable";
import { ServiceGrid } from "@/components/ServiceGrid";
import { api } from "@/lib/api";
import type { AccountOrdersResponse, AccountSummaryResponse, ApiResult, NiftyCandlesResponse, NiftyOptionChainResponse, NiftyQuoteResponse, SystemStatusResponse } from "@/lib/types";

type OperationalStatus = "BLOCKED" | "STALE" | "DEGRADED" | "MARKET_CLOSED" | "LIVE";
type PollGroup<T> = { data: T; status: OperationalStatus; reason: string | null; asOf: string | null };
type MarketData = { quote: NiftyQuoteResponse | null; options: NiftyOptionChainResponse | null; candles: NiftyCandlesResponse | null };
type AccountData = { account: AccountSummaryResponse | null; orders: AccountOrdersResponse | null };
type DashboardState = { system: PollGroup<SystemStatusResponse | null>; market: PollGroup<MarketData>; account: PollGroup<AccountData> };

const initialState: DashboardState = {
  system: { data: null, status: "STALE", reason: null, asOf: null },
  market: { data: { quote: null, options: null, candles: null }, status: "STALE", reason: null, asOf: null },
  account: { data: { account: null, orders: null }, status: "STALE", reason: null, asOf: null }
};

const severity: Record<OperationalStatus, number> = { BLOCKED: 0, STALE: 1, DEGRADED: 2, MARKET_CLOSED: 3, LIVE: 4 };

function statusFromSystem(value: string): OperationalStatus {
  const normalized = value.toUpperCase() as OperationalStatus;
  return normalized in severity ? normalized : "BLOCKED";
}

function issueFrom(results: ApiResult<unknown>[]): string | null {
  return results.find((result) => result.unavailable)?.unavailable?.code ?? results.find((result) => result.error)?.error ?? null;
}

function latestTimestamp(values: Array<string | null | undefined>): string | null {
  const validValues = values.filter((value): value is string => typeof value === "string" && !Number.isNaN(new Date(value).valueOf()));
  return validValues.length === 0 ? null : validValues.reduce((latest, value) => new Date(value).valueOf() > new Date(latest).valueOf() ? value : latest);
}

function resultTimestamp<T>(result: ApiResult<T>, sourceTimestamp: string | null | undefined): string | null | undefined {
  return sourceTimestamp ?? result.unavailable?.occurred_at;
}

function useSequentialPolling(interval: number, refresh: (signal: AbortSignal) => Promise<void>) {
  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;
    let controller: AbortController | null = null;
    const run = async () => {
      controller = new AbortController();
      await refresh(controller.signal);
      if (!cancelled) timer = window.setTimeout(run, interval);
    };
    void run();
    return () => {
      cancelled = true;
      controller?.abort();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [interval, refresh]);
}

export default function Page() {
  const [state, setState] = useState<DashboardState>(initialState);

  const refreshSystem = useCallback(async (signal: AbortSignal) => {
    const result = await api.systemStatus(signal);
    if (signal.aborted) return;
    const reason = issueFrom([result]);
    setState((current) => ({ ...current, system: { data: result.data, status: reason ? "BLOCKED" : result.data ? statusFromSystem(result.data.state) : "STALE", reason, asOf: resultTimestamp(result, result.data?.source_observed_at) ?? null } }));
  }, []);

  const refreshMarket = useCallback(async (signal: AbortSignal) => {
    const [quote, options, candles] = await Promise.all([api.niftyQuote(signal), api.niftyOptions(signal), api.niftyCandles(signal)]);
    if (signal.aborted) return;
    const results = [quote, options, candles];
    const reason = issueFrom(results);
    setState((current) => ({ ...current, market: { data: { quote: quote.data, options: options.data, candles: candles.data }, status: reason ? "BLOCKED" : "LIVE", reason, asOf: latestTimestamp([
      resultTimestamp(quote, quote.data ? latestTimestamp([quote.data.spot.source_observed_at, quote.data.nearest_future.source_observed_at]) : null),
      resultTimestamp(options, options.data?.source_observed_at),
      resultTimestamp(candles, candles.data ? latestTimestamp(candles.data.candles.map((candle) => candle.source_closed_at)) : null)
    ]) } }));
  }, []);

  const refreshAccount = useCallback(async (signal: AbortSignal) => {
    const [account, orders] = await Promise.all([api.accountSummary(signal), api.accountOrders(signal)]);
    if (signal.aborted) return;
    const results = [account, orders];
    const reason = issueFrom(results);
    setState((current) => ({ ...current, account: { data: { account: account.data, orders: orders.data }, status: reason ? "BLOCKED" : "LIVE", reason, asOf: latestTimestamp([
      resultTimestamp(account, account.data?.source_observed_at),
      resultTimestamp(orders, orders.data ? latestTimestamp([orders.data.orders_source_observed_at, orders.data.fills_source_observed_at]) : null)
    ]) } }));
  }, []);

  useSequentialPolling(2_000, refreshSystem);
  useSequentialPolling(3_000, refreshMarket);
  useSequentialPolling(15_000, refreshAccount);

  const globalGroup = [state.system, state.market, state.account].reduce((mostSevere, group) => severity[group.status] < severity[mostSevere.status] ? group : mostSevere);
  return <main><header className="page-header"><div><p className="eyebrow">Groww / NIFTY</p><h1>Live operations monitor</h1></div><p>Read-only decision support</p></header><GlobalStatus status={globalGroup.status} reason={globalGroup.reason ?? undefined} asOf={globalGroup.asOf} />{state.system.data ? <ServiceGrid services={state.system.data.services} /> : null}<div className="dashboard-grid"><MarketPanel quote={state.market.data.quote} /><AccountPanel account={state.account.data.account} orders={state.account.data.orders} /></div><CandlePanel candles={state.market.data.candles} /><OptionTable optionChain={state.market.data.options} /></main>;
}
