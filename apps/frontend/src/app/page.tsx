"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { AccountOrdersResponse, AccountSummaryResponse, NiftyCandlesResponse, NiftyOptionChainResponse, NiftyQuoteResponse, SystemStatusResponse } from "@/lib/types";
import { AccountPanel } from "@/components/AccountPanel";
import { CandlePanel } from "@/components/CandlePanel";
import { GlobalStatus } from "@/components/GlobalStatus";
import { MarketPanel } from "@/components/MarketPanel";
import { OptionTable } from "@/components/OptionTable";
import { ServiceGrid } from "@/components/ServiceGrid";

type DashboardState = { system: SystemStatusResponse | null; quote: NiftyQuoteResponse | null; options: NiftyOptionChainResponse | null; candles: NiftyCandlesResponse | null; account: AccountSummaryResponse | null; orders: AccountOrdersResponse | null; reason: string | null; asOf: string };

const initialState: DashboardState = { system: null, quote: null, options: null, candles: null, account: null, orders: null, reason: null, asOf: new Date(0).toISOString() };

export default function Page() {
  const [state, setState] = useState<DashboardState>(initialState);

  useEffect(() => {
    let active = true;
    const refreshSystem = async () => {
      const result = await api.systemStatus();
      if (!active) return;
      setState((current) => ({ ...current, system: result.data, reason: result.unavailable?.code ?? result.error, asOf: result.data?.source_observed_at ?? result.unavailable?.occurred_at ?? current.asOf }));
    };
    void refreshSystem();
    const timer = window.setInterval(() => void refreshSystem(), 2_000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  useEffect(() => {
    let active = true;
    const refreshMarket = async () => {
      const [quote, options, candles] = await Promise.all([api.niftyQuote(), api.niftyOptions(), api.niftyCandles()]);
      if (!active) return;
      setState((current) => ({ ...current, quote: quote.data, options: options.data, candles: candles.data, reason: current.reason ?? quote.unavailable?.code ?? options.unavailable?.code ?? candles.unavailable?.code ?? quote.error ?? options.error ?? candles.error }));
    };
    void refreshMarket();
    const timer = window.setInterval(() => void refreshMarket(), 3_000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  useEffect(() => {
    let active = true;
    const refreshAccount = async () => {
      const [account, orders] = await Promise.all([api.accountSummary(), api.accountOrders()]);
      if (!active) return;
      setState((current) => ({ ...current, account: account.data, orders: orders.data, reason: current.reason ?? account.unavailable?.code ?? orders.unavailable?.code ?? account.error ?? orders.error }));
    };
    void refreshAccount();
    const timer = window.setInterval(() => void refreshAccount(), 15_000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  const status = state.reason ? "BLOCKED" : state.system?.state ?? "STALE";
  return <main><header className="page-header"><div><p className="eyebrow">Groww / NIFTY</p><h1>Live operations monitor</h1></div><p>Read-only decision support</p></header><GlobalStatus status={status} reason={state.reason ?? undefined} asOf={state.asOf} />{state.system ? <ServiceGrid services={state.system.services} /> : null}<div className="dashboard-grid"><MarketPanel quote={state.quote} /><AccountPanel account={state.account} orders={state.orders} /></div><CandlePanel candles={state.candles} /><OptionTable optionChain={state.options} /></main>;
}
