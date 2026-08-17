"use client";

import { useEffect, useState, type Dispatch, type SetStateAction } from "react";

import { StatusBanner } from "../lib/status";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/backend";
const FAST_ENDPOINTS = [
  "/health/ready",
  "/api/v1/system/status",
  "/api/v1/market/nifty",
  "/api/v1/market/nifty/options",
  "/api/v1/market/nifty/candles?interval=1m",
  "/api/v1/market/nifty/candles?interval=5m",
] as const;
const ACCOUNT_ENDPOINTS = [
  "/api/v1/account/summary",
  "/api/v1/account/orders",
] as const;
const ENDPOINTS = [...FAST_ENDPOINTS, ...ACCOUNT_ENDPOINTS] as const;

type Endpoint = (typeof ENDPOINTS)[number];
type SourceStamped = { source?: string; source_at?: string };
type Resource<T = unknown> = {
  data?: T;
  reason?: string;
  lastSuccessAt?: string | null;
};
type Resources = Record<Endpoint, Resource>;
type Instrument = {
  trading_symbol: string;
  option_type?: "CE" | "PE" | null;
  strike?: string | number | null;
};
type Quote = SourceStamped & { instrument?: Instrument; price?: string | number | null };
type Market = SourceStamped & { spot?: Quote; future?: Quote | null };
type OptionContract = SourceStamped & {
  instrument: Instrument;
  ltp?: string | number | null;
  bid?: string | number | null;
  ask?: string | number | null;
  spread?: string | number | null;
  volume?: string | number | null;
  open_interest?: string | number | null;
  implied_volatility?: string | number | null;
  delta?: string | number | null;
  gamma?: string | number | null;
  theta?: string | number | null;
  vega?: string | number | null;
};
type Options = SourceStamped & {
  expiry?: string;
  atm_strike?: string | number | null;
  contracts?: OptionContract[];
};
type Candle = {
  bucket_start: string;
  open: string | number;
  high: string | number;
  low: string | number;
  close: string | number;
  volume?: string | number | null;
  finalized?: boolean;
};
type Candles = SourceStamped & { interval?: string; candles?: Candle[] };
type Subsystem = {
  subsystem?: string;
  state?: string;
  reason?: string | null;
  source_at?: string | null;
  last_success_at?: string | null;
};
type System = SourceStamped & {
  state?: string;
  reason?: string | null;
  observed_at?: string;
  subsystems?: Subsystem[];
};
type Position = {
  trading_symbol?: string;
  quantity?: string | number;
  average_price?: string | number | null;
  last_price?: string | number | null;
  pnl?: string | number | null;
};
type Account = SourceStamped & {
  available_margin?: string | number | null;
  used_margin?: string | number | null;
  positions?: Position[];
};
type Fill = {
  trade_id?: string;
  order_id?: string;
  segment?: string;
  quantity?: string | number;
  price?: string | number;
  source_at?: string;
};
type Order = {
  order_id?: string;
  segment?: string;
  trading_symbol?: string;
  status?: string;
  quantity?: string | number;
  price?: string | number | null;
  source_at?: string;
  fills?: Fill[];
};
type Orders = SourceStamped & { orders?: Order[] };

const INITIAL_REASON = "WAITING_FOR_LIVE_DATA";
const initialResources = Object.fromEntries(
  ENDPOINTS.map((endpoint) => [endpoint, { reason: INITIAL_REASON }]),
) as Resources;

function usePolling(
  endpoints: readonly Endpoint[],
  interval: number,
  setResources: Dispatch<SetStateAction<Resources>>,
) {
  useEffect(() => {
    let active = true;

    async function poll() {
      const updates = await Promise.all(
        endpoints.map(async (endpoint): Promise<[Endpoint, Resource]> => {
          try {
            const response = await fetch(`${API_BASE}${endpoint}`, {
              cache: "no-store",
              signal: AbortSignal.timeout(2_500),
            });
            const body: unknown = await response.json();
            if (!response.ok) {
              const error = body as { reason?: unknown; last_success_at?: unknown };
              return [
                endpoint,
                {
                  reason: typeof error.reason === "string" ? error.reason : `HTTP_${response.status}`,
                  lastSuccessAt:
                    typeof error.last_success_at === "string" ? error.last_success_at : null,
                },
              ];
            }
            return [endpoint, { data: body }];
          } catch {
            return [endpoint, { reason: "API_UNAVAILABLE" }];
          }
        }),
      );
      if (active) {
        setResources((current) => ({ ...current, ...Object.fromEntries(updates) }));
      }
    }

    void poll();
    const timer = window.setInterval(poll, interval);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [endpoints, interval, setResources]);
}

function value(input: unknown): string {
  return input === null || input === undefined || input === "" ? "—" : String(input);
}

function time(input: unknown): string {
  if (typeof input !== "string") return "—";
  const parsed = new Date(input);
  if (Number.isNaN(parsed.valueOf())) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: "Asia/Kolkata",
  }).format(parsed);
}

function source(resource: Resource<SourceStamped>): string {
  return `${value(resource.data?.source)} · ${time(resource.data?.source_at ?? resource.lastSuccessAt)}`;
}

function sameMarketDay(input?: string): boolean {
  if (!input) return false;
  const formatter = new Intl.DateTimeFormat("en-CA", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    timeZone: "Asia/Kolkata",
  });
  return formatter.format(new Date(input)) === formatter.format(new Date());
}

function DataNotice({ resource }: { resource: Resource }) {
  return resource.reason ? (
    <p className="data-notice" role="status">
      BLOCKED · {resource.reason} · Last success {time(resource.lastSuccessAt)}
    </p>
  ) : null;
}

function QuoteCard({ label, quote }: { label: string; quote?: Quote | null }) {
  return (
    <article className="metric-card">
      <p className="eyebrow">{label}</p>
      <h3>{value(quote?.instrument?.trading_symbol)}</h3>
      <p className="metric-value">{value(quote?.price)}</p>
      <p className="source-line">
        {value(quote?.source)} · {time(quote?.source_at)}
      </p>
    </article>
  );
}

function CandleTable({ title, resource }: { title: string; resource: Resource<Candles> }) {
  const rows = resource.data?.candles?.slice(-8) ?? [];
  return (
    <article className="panel table-panel">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Recent finalized bars</p>
          <h3>{title}</h3>
        </div>
        <p className="source-line">{source(resource)}</p>
      </div>
      <DataNotice resource={resource} />
      <div className="table-scroll">
        <table>
          <caption>{title} OHLC values</caption>
          <thead>
            <tr>
              <th scope="col">Bucket</th>
              <th scope="col">Open</th>
              <th scope="col">High</th>
              <th scope="col">Low</th>
              <th scope="col">Close</th>
              <th scope="col">Volume</th>
            </tr>
          </thead>
          <tbody>
            {rows.length ? (
              rows.map((row) => (
                <tr key={row.bucket_start}>
                  <th scope="row">{time(row.bucket_start)}</th>
                  <td>{value(row.open)}</td>
                  <td>{value(row.high)}</td>
                  <td>{value(row.low)}</td>
                  <td>{value(row.close)}</td>
                  <td>{value(row.volume)}</td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={6} className="empty">No live candle data —</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </article>
  );
}

export default function Dashboard() {
  const [resources, setResources] = useState<Resources>(initialResources);
  usePolling(FAST_ENDPOINTS, 3_000, setResources);
  usePolling(ACCOUNT_ENDPOINTS, 15_000, setResources);

  const statusResource = resources["/api/v1/system/status"] as Resource<System>;
  const marketResource = resources["/api/v1/market/nifty"] as Resource<Market>;
  const optionsResource = resources["/api/v1/market/nifty/options"] as Resource<Options>;
  const oneMinuteResource = resources[
    "/api/v1/market/nifty/candles?interval=1m"
  ] as Resource<Candles>;
  const fiveMinuteResource = resources[
    "/api/v1/market/nifty/candles?interval=5m"
  ] as Resource<Candles>;
  const accountResource = resources["/api/v1/account/summary"] as Resource<Account>;
  const ordersResource = resources["/api/v1/account/orders"] as Resource<Orders>;
  const failures = ENDPOINTS.flatMap((endpoint) =>
    resources[endpoint].reason ? [`${endpoint}: ${resources[endpoint].reason}`] : [],
  );
  const subsystemReason = statusResource.data?.subsystems?.find((item) => item.reason)?.reason;
  const globalState = statusResource.data?.state ?? "BLOCKED";
  const globalReason = failures.join(" · ") || statusResource.data?.reason || subsystemReason;
  const sourceAt = statusResource.data?.source_at ?? statusResource.data?.observed_at ?? null;
  const contracts = [...(optionsResource.data?.contracts ?? [])].sort(
    (left, right) =>
      Number(left.instrument.strike) - Number(right.instrument.strike) ||
      value(left.instrument.option_type).localeCompare(value(right.instrument.option_type)),
  );
  const positions = accountResource.data?.positions ?? [];
  const orders = ordersResource.data?.orders ?? [];
  const todaysOrders = orders.filter((order) => sameMarketDay(order.source_at));
  const todaysFills = orders
    .flatMap((order) => order.fills ?? [])
    .filter((fill) => sameMarketDay(fill.source_at));

  return (
    <main>
      <header className="page-header">
        <div>
          <p className="eyebrow">Read-only operations dashboard</p>
          <h1>NIFTY live foundation</h1>
        </div>
        <p className="poll-note">Market refresh 3s · Account refresh 15s</p>
      </header>

      <StatusBanner state={globalState} sourceAt={time(sourceAt)} reason={globalReason} />

      <section aria-labelledby="health-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Dependencies and feed</p>
            <h2 id="health-heading">Subsystem health</h2>
          </div>
          <p className="source-line">{source(statusResource)}</p>
        </div>
        <div className="health-grid">
          {statusResource.data?.subsystems?.length ? (
            statusResource.data.subsystems.map((item, index) => (
              <article className="health-card" key={`${item.subsystem}-${index}`}>
                <h3>{value(item.subsystem)}</h3>
                <p className={`state-text state-${(item.state ?? "BLOCKED").toLowerCase()}`}>
                  {value(item.state ?? "BLOCKED")}
                </p>
                <p>{item.reason ? `Reason: ${item.reason}` : "Reason: —"}</p>
                <p className="source-line">
                  Source time {time(item.source_at ?? item.last_success_at)}
                </p>
              </article>
            ))
          ) : (
            <article className="health-card">
              <h3>System API</h3>
              <p className="state-text state-blocked">BLOCKED</p>
              <p>Reason: {statusResource.reason ?? INITIAL_REASON}</p>
              <p className="source-line">Source time {time(statusResource.lastSuccessAt)}</p>
            </article>
          )}
        </div>
      </section>

      <section aria-labelledby="market-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Normalized live quotes</p>
            <h2 id="market-heading">NIFTY market</h2>
          </div>
          <p className="source-line">{source(marketResource)}</p>
        </div>
        <DataNotice resource={marketResource} />
        <div className="metric-grid">
          <QuoteCard label="Spot" quote={marketResource.data?.spot} />
          <QuoteCard label="Nearest eligible future" quote={marketResource.data?.future} />
        </div>
      </section>

      <section className="panel table-panel" aria-labelledby="options-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">
              Expiry {value(optionsResource.data?.expiry)} · ATM {value(optionsResource.data?.atm_strike)}
            </p>
            <h2 id="options-heading">ATM ±5 option window</h2>
          </div>
          <p className="source-line">{source(optionsResource)}</p>
        </div>
        <DataNotice resource={optionsResource} />
        <div className="table-scroll">
          <table>
            <caption>Call and put contracts around the live ATM strike</caption>
            <thead>
              <tr>
                <th scope="col">Strike</th>
                <th scope="col">Type</th>
                <th scope="col">Symbol</th>
                <th scope="col">LTP</th>
                <th scope="col">Bid</th>
                <th scope="col">Ask</th>
                <th scope="col">Spread</th>
                <th scope="col">OI</th>
                <th scope="col">Volume</th>
                <th scope="col">IV</th>
                <th scope="col">Delta</th>
                <th scope="col">Gamma</th>
                <th scope="col">Theta</th>
                <th scope="col">Vega</th>
              </tr>
            </thead>
            <tbody>
              {contracts.length ? (
                contracts.map((contract) => (
                  <tr key={contract.instrument.trading_symbol}>
                    <th scope="row">{value(contract.instrument.strike)}</th>
                    <td>{value(contract.instrument.option_type)}</td>
                    <td>{value(contract.instrument.trading_symbol)}</td>
                    <td>{value(contract.ltp)}</td>
                    <td>{value(contract.bid)}</td>
                    <td>{value(contract.ask)}</td>
                    <td>{value(contract.spread)}</td>
                    <td>{value(contract.open_interest)}</td>
                    <td>{value(contract.volume)}</td>
                    <td>{value(contract.implied_volatility)}</td>
                    <td>{value(contract.delta)}</td>
                    <td>{value(contract.gamma)}</td>
                    <td>{value(contract.theta)}</td>
                    <td>{value(contract.vega)}</td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={14} className="empty">No live option data —</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section aria-labelledby="candles-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Durable reconstructed history</p>
            <h2 id="candles-heading">Candles</h2>
          </div>
        </div>
        <div className="candle-grid">
          <CandleTable title="1-minute candles" resource={oneMinuteResource} />
          <CandleTable title="5-minute candles" resource={fiveMinuteResource} />
        </div>
      </section>

      <section aria-labelledby="account-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Reconciled account state</p>
            <h2 id="account-heading">Margin and positions</h2>
          </div>
          <p className="source-line">{source(accountResource)}</p>
        </div>
        <DataNotice resource={accountResource} />
        <div className="metric-grid">
          <article className="metric-card">
            <p className="eyebrow">Available margin</p>
            <p className="metric-value">{value(accountResource.data?.available_margin)}</p>
          </article>
          <article className="metric-card">
            <p className="eyebrow">Used margin</p>
            <p className="metric-value">{value(accountResource.data?.used_margin)}</p>
          </article>
        </div>
        <div className="panel table-panel">
          <div className="table-scroll">
            <table>
              <caption>Current reconciled positions</caption>
              <thead>
                <tr>
                  <th scope="col">Symbol</th>
                  <th scope="col">Quantity</th>
                  <th scope="col">Average price</th>
                  <th scope="col">Last price</th>
                  <th scope="col">P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {positions.length ? (
                  positions.map((position, index) => (
                    <tr key={`${position.trading_symbol}-${index}`}>
                      <th scope="row">{value(position.trading_symbol)}</th>
                      <td>{value(position.quantity)}</td>
                      <td>{value(position.average_price)}</td>
                      <td>{value(position.last_price)}</td>
                      <td>{value(position.pnl)}</td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={5} className="empty">No live position data —</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section aria-labelledby="orders-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Current exchange day</p>
            <h2 id="orders-heading">Orders and fills</h2>
          </div>
          <p className="source-line">{source(ordersResource)}</p>
        </div>
        <DataNotice resource={ordersResource} />
        <div className="orders-grid">
          <article className="panel table-panel">
            <div className="table-scroll">
              <table>
                <caption>Current-day reconciled orders</caption>
                <thead>
                  <tr>
                    <th scope="col">Time</th>
                    <th scope="col">ID</th>
                    <th scope="col">Segment</th>
                    <th scope="col">Symbol</th>
                    <th scope="col">Status</th>
                    <th scope="col">Quantity</th>
                    <th scope="col">Price</th>
                  </tr>
                </thead>
                <tbody>
                  {todaysOrders.length ? (
                    todaysOrders.map((order) => (
                      <tr key={order.order_id}>
                        <th scope="row">{time(order.source_at)}</th>
                        <td>{value(order.order_id)}</td>
                        <td>{value(order.segment)}</td>
                        <td>{value(order.trading_symbol)}</td>
                        <td>{value(order.status)}</td>
                        <td>{value(order.quantity)}</td>
                        <td>{value(order.price)}</td>
                      </tr>
                    ))
                  ) : (
                    <tr>
                      <td colSpan={7} className="empty">No current-day order data —</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </article>
          <article className="panel table-panel">
            <div className="table-scroll">
              <table>
                <caption>Current-day reconciled fills</caption>
                <thead>
                  <tr>
                    <th scope="col">Time</th>
                    <th scope="col">Trade ID</th>
                    <th scope="col">Order ID</th>
                    <th scope="col">Segment</th>
                    <th scope="col">Quantity</th>
                    <th scope="col">Price</th>
                  </tr>
                </thead>
                <tbody>
                  {todaysFills.length ? (
                    todaysFills.map((fill) => (
                      <tr key={fill.trade_id}>
                        <th scope="row">{time(fill.source_at)}</th>
                        <td>{value(fill.trade_id)}</td>
                        <td>{value(fill.order_id)}</td>
                        <td>{value(fill.segment)}</td>
                        <td>{value(fill.quantity)}</td>
                        <td>{value(fill.price)}</td>
                      </tr>
                    ))
                  ) : (
                    <tr>
                      <td colSpan={6} className="empty">No current-day fill data —</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </article>
        </div>
      </section>
    </main>
  );
}
