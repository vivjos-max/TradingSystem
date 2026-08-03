# Live Read-Only Foundation Design

**Date:** 2026-08-03

**Status:** Approved in conversation; awaiting review of this written specification

**Scope:** First independently testable sub-project of the NIFTY decision-support and risk-control system

## 1. Outcome

Build a locally runnable, live-data foundation that proves the application can authenticate with Groww, read current NIFTY and account state, consume Groww's streaming feed, construct reproducible candles, persist the normalized data, and display operational health in a private dashboard.

This release is successful only when it processes real responses from the user's subscribed Groww account. It must never substitute sample or delayed fixture data when live authentication, subscription, or feed connectivity is unavailable. Instead, it presents an explicit `BLOCKED` or `STALE` state with the reason.

This release is analysis-only. It contains no order-placement, modification, cancellation, smart-order, or other execution-capable application method. It also contains no AI reviewer. Those capabilities belong to later, separately approved sub-projects.

## 2. Why This Is the First Sub-Project

The broader product contains several independently risky systems: broker connectivity, market-data normalization, deterministic analytics, risk controls, AI review, order preparation, execution, and journal analytics. Building them together would make data errors difficult to isolate and could expose execution behavior before market and account state are trustworthy.

The live read-only foundation establishes the source-of-truth boundary first. Later calculations and reviews will consume normalized facts from this boundary rather than screenshots, model-generated numbers, or unverified webhook payloads.

## 3. User Preconditions

Before the live acceptance test, the user must:

1. Have an active Groww account with the relevant market segments enabled.
2. Purchase an active Groww Trading API subscription.
3. Generate TOTP credentials from the Groww Cloud API Keys page.
4. Place the TOTP token and secret in the local environment file created from `.env.example`.

Credentials must never be pasted into chat, committed to Git, returned by an API endpoint, written to application logs, or sent to the browser. The backend generates an access token in memory. Secret-bearing configuration fields are represented as secret types and are redacted from diagnostics.

## 4. Fixed Technology Choices

- **Frontend:** Next.js with TypeScript.
- **Backend:** Python 3.11+ with FastAPI.
- **Broker integration:** Official `growwapi` Python SDK.
- **Durable storage:** PostgreSQL.
- **Live state and heartbeat cache:** Redis.
- **Local orchestration:** Docker Compose.
- **Time zone:** All stored timestamps use UTC; exchange-session logic and displayed market times use `Asia/Kolkata`.
- **Package boundaries:** The frontend never imports broker concepts directly. It consumes normalized backend schemas.

PostgreSQL is sufficient for this slice. TimescaleDB is deferred until measured data volume or query performance justifies it.

## 5. Architecture

```text
Groww REST API                    Groww streaming feed
      |                                   |
      v                                   v
Groww read-only adapter           Feed worker + heartbeat
      |                                   |
      +--------------+--------------------+
                     v
             Normalization boundary
                     |
          +----------+-----------+
          |                      |
          v                      v
     PostgreSQL             Redis live state
          |                      |
          +----------+-----------+
                     v
                 FastAPI
                     |
                     v
            Next.js status dashboard
```

The backend is split into focused modules:

- `settings`: validates environment configuration without leaking secrets.
- `groww/auth`: creates an authenticated SDK client through TOTP.
- `groww/read_client`: exposes only approved read operations.
- `groww/normalizers`: converts SDK payloads into stable internal schemas.
- `market/candles`: constructs time-bucketed OHLCV candles deterministically.
- `market/freshness`: evaluates quote, feed, and account-state freshness.
- `market/session`: determines exchange-open, exchange-closed, and calendar-unknown states.
- `storage`: persists source snapshots, normalized observations, and candles.
- `worker`: owns subscriptions, callbacks, reconnect behavior, and heartbeats.
- `api`: exposes health, market, account, and candle views to the frontend.

The `groww/read_client` interface intentionally has no generic SDK passthrough and no order-related operation. This compile-time design boundary is the primary execution safeguard for this release.

## 6. Live Data Scope

The application reads and normalizes:

- NIFTY spot/index quote.
- The nearest eligible NIFTY future identified from the current instrument master.
- The nearest eligible NIFTY option expiry.
- A bounded option-chain window around the current ATM strike.
- Contract LTP, bid, ask, spread, volume, open interest, implied volatility, and broker-supplied Greeks when present.
- Current margin/balance summary.
- Current positions.
- Current-day orders and fills for reconciliation only.
- Streaming LTP updates for the selected NIFTY instruments.

Instrument symbols, exchange tokens, expiry dates, strikes, lot sizes, tick sizes, and trade permissions come from Groww's instrument master. No strike or lot-size constant is hard-coded.

The initial option-chain window is the ATM strike plus five strikes on either side for calls and puts. The selection is refreshed when the ATM strike changes or the selected expiry becomes ineligible. The complete raw Groww option-chain response may be retained for audit, but only the bounded normalized window is kept in Redis and served to the dashboard.

## 7. Candle Construction

The feed worker creates one-minute candles from normalized live updates. Each observation is assigned to a half-open exchange-time interval: `[minute_start, minute_start + 1 minute)`.

For each candle:

- `open` is the first accepted price in event-time order.
- `high` and `low` are extrema of accepted prices.
- `close` is the last accepted price in event-time order.
- `volume` is derived only when the feed provides a trustworthy cumulative-volume field; otherwise it is recorded as unavailable rather than fabricated.
- late observations for an already finalized interval are persisted for audit and marked late, but do not silently rewrite a displayed final candle.

Five-minute candles are aggregated from finalized one-minute candles, not independently from a second stream. This makes their provenance reproducible.

Historical Groww candles may be fetched to warm the dashboard, but they are stored with a distinct `GROWW_HISTORICAL` source and are never presented as locally reconstructed candles.

## 8. Freshness and Operational States

Each subsystem reports one of:

- `HEALTHY`: authenticated and current within its defined threshold.
- `DEGRADED`: connected but missing a non-critical field or recovering.
- `STALE`: last valid update exceeds its freshness threshold.
- `BLOCKED`: configuration, entitlement, authentication, or required upstream data is unavailable.

The dashboard derives one global label from those subsystem states:

- `BLOCKED` when any required subsystem is blocked.
- `STALE` when the market is open and any required live source is stale.
- `DEGRADED` when no required subsystem is blocked or stale but at least one is degraded.
- `MARKET CLOSED` when the exchange calendar says the regular session is closed and the last required snapshots are internally consistent.
- `LIVE` only when the regular session is open and all required subsystems are healthy.

Initial thresholds:

- Feed heartbeat stale after 10 seconds during the regular exchange session.
- Live quote stale after 15 seconds during the regular exchange session.
- Account snapshot stale after 60 seconds while the dashboard is active.
- Database or Redis health checks time out after 2 seconds.

Regular-session decisions use `Asia/Kolkata` and a versioned exchange calendar that includes weekends, exchange holidays, and exceptional sessions. If the calendar has no authoritative entry for the current date, the global state is `BLOCKED` with a calendar-unknown reason. The application does not infer an ordinary trading day from Monday-to-Friday alone.

Outside the regular exchange session, the UI labels prices with their last exchange timestamp and reports the global state as `MARKET CLOSED`; it does not classify an otherwise valid last close as a feed failure. Candle aggregation never crosses an exchange-session boundary.

Any SDK exception is mapped to a stable internal error code. Raw broker errors may be logged only after secret redaction. The browser receives a safe summary, correlation ID, affected subsystem, and last-success timestamp.

## 9. Persistence

PostgreSQL stores:

- instrument-master imports and their source timestamps;
- raw broker snapshots as JSON for audit;
- normalized quotes and option-chain observations;
- raw streaming observations needed to reproduce candles;
- finalized one-minute and five-minute candles;
- account snapshots for margin, positions, orders, and fills;
- service-health transitions and correlation IDs.

Writes use broker/event identifiers where available and deterministic uniqueness keys otherwise. Reprocessing an observation is idempotent. Duplicate feed callbacks cannot create duplicate source observations or candles.

Redis stores only replaceable live state:

- latest normalized quotes;
- bounded option-chain window;
- current in-progress candles;
- subsystem heartbeats;
- latest account-state summary;
- subscription and reconnect state.

Redis loss must not destroy durable history. The worker rebuilds live state from the latest PostgreSQL records and fresh Groww reads after reconnecting.

## 10. Backend API

The first release exposes only read endpoints:

- `GET /health/live` — process liveness.
- `GET /health/ready` — aggregate dependency and subscription readiness.
- `GET /api/v1/system/status` — redacted subsystem states and timestamps.
- `GET /api/v1/market/nifty` — normalized spot/future snapshot.
- `GET /api/v1/market/nifty/options` — bounded current option-chain window.
- `GET /api/v1/market/nifty/candles?interval=1m|5m` — recent candles with provenance.
- `GET /api/v1/account/summary` — redacted margin and position summary.
- `GET /api/v1/account/orders` — current-day order and fill reconciliation view.

No route accepts an order-shaped command. FastAPI's generated OpenAPI document must contain no write path and no order-placement schema.

The initial browser update mechanism is short-interval polling. A browser-facing WebSocket or server-sent-events channel is deferred because it does not improve the broker-data proof and adds another reconnection system. The backend still uses Groww's streaming feed.

## 11. Dashboard

The private dashboard is an operational proof, not the final trading interface. It contains:

- a prominent global `LIVE / DEGRADED / STALE / BLOCKED / MARKET CLOSED` banner;
- Groww authentication, REST, stream, PostgreSQL, and Redis status cards;
- NIFTY spot and nearest-future values with source timestamps;
- a compact ATM-centered option table;
- one-minute and five-minute candle tables or compact charts with provenance;
- margin and current-position summary;
- current-day order/fill reconciliation view;
- safe diagnostic messages and correlation IDs.

There are no `Approve`, `Buy`, `Sell`, `Exit`, or order-ticket controls in this release.

Because the dashboard is private but may be deployed later, the API binds to localhost by default and the browser never receives Groww credentials or access tokens. Production authentication and remote hosting are separate work and must be completed before exposing the dashboard beyond localhost.

## 12. Startup and Reconciliation

Startup proceeds in this order:

1. Validate non-secret and secret configuration.
2. Confirm PostgreSQL and Redis connectivity.
3. Generate a Groww access token through TOTP.
4. Fetch the instrument master and resolve NIFTY instruments.
5. Read margin, positions, current-day orders, and fills.
6. Persist the initial account and market snapshots.
7. Subscribe to the selected live instruments.
8. Mark the application ready only after the first valid live update arrives.

If any required step fails, readiness remains false and the exact subsystem is `BLOCKED`. The service may continue retrying safe read operations with bounded exponential backoff and jitter, but never converts a failed live start into a simulated mode.

On every restart, the application re-reads positions, orders, and fills before claiming readiness. It never assumes the account is flat.

## 13. Testing Strategy

### Unit tests

- Environment validation and secret redaction.
- Groww payload normalization, including missing optional fields.
- Instrument selection for ATM, nearest future, nearest expiry, lot size, and tick size.
- UTC and `Asia/Kolkata` conversion around candle boundaries.
- One-minute candle open/high/low/close calculation.
- Five-minute aggregation from finalized one-minute candles.
- Duplicate and late-event handling.
- Freshness state transitions during market-open and market-closed periods.
- Broker exception-to-error-code mapping.

### Contract tests

Recorded, secret-free Groww response shapes validate the adapter without becoming a runtime fallback. Contract fixtures are permitted only in tests.

The generated OpenAPI schema is tested to prove it contains no non-`GET` endpoint and no execution-related route.

### Integration tests

Docker-based tests verify PostgreSQL idempotency, Redis recovery, and API readiness behavior.

### Live acceptance test

With the user's subscription and local TOTP environment configured:

1. Authentication succeeds without logging credentials.
2. NIFTY spot, future, and option-chain values match Groww within timestamp tolerance.
3. Account margin, positions, orders, and fills match Groww.
4. Streaming observations arrive and heartbeats remain healthy.
5. Locally reconstructed one-minute candles agree with the accepted raw observations.
6. Five-minute candles equal aggregation of the corresponding finalized one-minute candles.
7. Disconnecting the feed produces `STALE`, not frozen-looking `LIVE` data.
8. Restarting forces account reconciliation before readiness.
9. The OpenAPI document exposes no execution operation.

Live tests are explicitly labelled and excluded from ordinary CI so credentials are never required in shared automation. A live acceptance report records timestamps and result summaries without secret or complete account data.

## 14. Deliverables

- Docker Compose services for frontend, API, worker, PostgreSQL, and Redis.
- An ignored local `.env` workflow plus a safe `.env.example`.
- Database migrations for the first-release persistence model.
- Read-only Groww adapter and normalization layer.
- Feed worker, candle builder, freshness engine, and startup reconciliation.
- Read-only FastAPI endpoints.
- Minimal Next.js operational dashboard.
- Automated unit, contract, integration, and OpenAPI safety tests.
- Setup, credential-handling, startup, verification, and troubleshooting documentation.

## 15. Explicitly Deferred

- Order placement, modification, cancellation, GTT, OCO, or execution credentials.
- TradingView webhooks.
- Technical indicators, setup detection, strike recommendation, cost calculation, and position sizing.
- OpenAI API integration and AI review.
- Trade approval workflow.
- Shadow orders and simulated OCO exits.
- Trade journal and performance analytics.
- Backtesting and a separate long-history data provider.
- TimescaleDB.
- Remote deployment, public ingress, user authentication, and Groww Cloud execution.

Each deferred capability requires its own design, tests, and approval. In particular, adding any execution method requires an explicit risk-engine design and cannot be introduced as incidental broker-adapter functionality.

## 16. Authoritative External Contracts

- [Groww Python SDK introduction and authentication](https://groww.in/trade-api/docs/python-sdk)
- [Groww live-data, option-chain, and Greeks methods](https://groww.in/trade-api/docs/python-sdk/live-data)
- [Groww streaming feed](https://groww.in/trade-api/docs/python-sdk/feed)
- [Groww instruments master](https://groww.in/trade-api/docs/python-sdk/instruments)
- [Groww portfolio and positions](https://groww.in/trade-api/docs/python-sdk/portfolio)
- [Groww orders and fills](https://groww.in/trade-api/docs/python-sdk/orders)

The implementation must pin and record the installed SDK version. If the installed SDK differs from these documented contracts, the live adapter must fail visibly until the discrepancy is reconciled; it must not guess field mappings.
