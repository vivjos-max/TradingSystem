# GAN Build Spec — Live Read-Only Groww Foundation

## Source of truth

- `docs/superpowers/specs/2026-08-03-live-read-only-foundation-design.md`
- `plans/live-read-only-foundation-mvp.md`

When these differ, the approved design's safety constraints win. The blueprint controls implementation order and deliberately small choices.

## Required outcome

Build a localhost-only FastAPI + Next.js system that authenticates to Groww through TOTP, reads and normalizes current NIFTY/account data, consumes the official live feed, reconstructs auditable 1m/5m candles, stores durable events in PostgreSQL and replaceable state in Redis, and displays explicit operational health.

## Must-have slices

1. Secret-safe settings, normalized models, NIFTY universe selection, exchange calendar, candle/freshness logic.
2. Explicit read-only Groww REST/feed adapter, including segmented order/fill pagination and worker-only feed credential-file isolation for `growwapi==1.5.0`.
3. Idempotent PostgreSQL event/candle storage and rebuildable Redis live state.
4. Worker reconciliation, LTP/index/market-depth subscriptions, steady refresh, readiness, and exactly eight GET endpoints.
5. One-page accessible dashboard, five-service localhost Compose topology, container isolation, and credentialed live-acceptance tooling.

## Locked safety behavior

- No execution-capable broker method, generic SDK passthrough, mutation API, or order control.
- No runtime fixture/sample fallback. Missing live prerequisites produce `BLOCKED`, `STALE`, `DEGRADED`, or `MARKET_CLOSED`.
- Credentials never enter Git, logs, database/cache, API/browser, or persistent container storage.
- OpenAPI contains only GET/HEAD operations; `GET /api/v1/account/orders` is reconciliation-only.
- Readiness requires exhaustive account reconciliation plus the first valid live feed event.

## Explicit non-goals

Execution, signals/indicators, AI review, backtesting, remote deployment/auth, TimescaleDB, browser sockets, chart/state/design-system libraries, and speculative extension points.

## Verification boundary

Unit, contract, OpenAPI, UI-safety, frontend-build, and static security checks must pass locally. PostgreSQL/Redis integration, Compose, and credentialed market-hours checks remain blocked—not skipped as success—when Docker or live prerequisites are unavailable.
