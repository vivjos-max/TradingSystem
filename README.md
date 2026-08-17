# Trading System — Live Read-Only Foundation

This MVP reads and displays live NIFTY and account state from Groww. It cannot place, modify, or cancel an order. Missing credentials, dependencies, calendar coverage, or current feed data produce an explicit operational failure state; the runtime never substitutes fixtures.

## Prerequisites

- Docker Desktop with Compose
- An active Groww Trading API subscription and enabled market segments
- Groww TOTP token and secret
- An exchange-open window for the full live-feed acceptance check

## Configure and run

```powershell
$envFile = Join-Path $env:USERPROFILE ".config\TradingSystem\live.env"
New-Item -ItemType Directory -Force (Split-Path $envFile) | Out-Null
Copy-Item .env.example $envFile
icacls $envFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)"
# Edit $envFile locally. Keep it outside OneDrive and never paste its values into chat.
docker compose --env-file $envFile up -d --build
docker compose --env-file $envFile ps
```

Open <http://127.0.0.1:3000>. The read API is at <http://127.0.0.1:8000/openapi.json>. Only those two localhost ports are published.

Startup is deliberately strict: migrations, PostgreSQL and Redis must be healthy, account reconciliation must finish, subscriptions must be active, and one valid live feed event must arrive before readiness becomes true. The browser calls the API through the same-origin `/backend` route; no account endpoint is bound to the LAN.

## Local verification

```powershell
scripts\bootstrap.ps1
.venv\Scripts\python.exe -m pytest apps\backend\tests\unit apps\backend\tests\contract -q
pnpm --dir apps\frontend install --frozen-lockfile
node --experimental-strip-types --test apps\frontend\src\tests\status.test.ts
pnpm --dir apps\frontend build
docker compose -f compose.yaml -f compose.test.yaml run --rm backend-test
scripts\live-acceptance.ps1 -EnvFile $envFile
```

The Docker test target runs on Python 3.12 and includes the real PostgreSQL/Redis integration test. Test fixtures exist only in that target; the API and worker runtime image does not copy them.

## Operational states

- `LIVE`: market open and all required live sources are healthy.
- `DEGRADED`: a non-critical value such as one side of market depth is missing.
- `STALE`: a required open-session feed or snapshot exceeded its freshness limit.
- `BLOCKED`: configuration, authentication, entitlement, calendar, or dependency is unavailable.
- `MARKET_CLOSED`: the authoritative exchange calendar says the regular session is closed.

## Troubleshooting

- `GROWW_CONFIG_MISSING`: set both Groww TOTP values in the private external env file.
- `GROWW_SDK_VERSION_UNSUPPORTED`: keep `growwapi==1.5.0`; a different release requires contract review.
- `CALENDAR_UNKNOWN`: add a reviewed NSE session file before running outside 2026 coverage.
- PostgreSQL/Redis unavailable: install/start Docker Desktop and inspect `docker compose ps`.
- No feed during an open session: verify API subscription, segment entitlement, and local connectivity; do not add fixture fallback.

PostgreSQL uses a named durable volume. After live data exists, do not run `docker compose down -v`. Alembic migrations are forward-only; restore a verified backup for recovery.

## Phase 0 invariants

- One worker process owns exactly one Groww feed generation. Feed death or silence makes the worker unhealthy and exits non-zero so Compose creates a fresh process.
- Accepted state is published only after durable event persistence. The bounded queue defaults to 4,096 events with batches of 100; queue overflow or persistence failure terminates the worker rather than dropping authoritative state.
- Default freshness limits are feed 10s, quotes 15s, options/account 60s, and source clock skew 5s. These protect operational correctness and remain configurable; they are not strategy thresholds.
- Retention is disabled in Phase 0. The approved future policy is depth current+previous session, raw events five completed sessions, and indefinite Phase 0 candle/account/order/fill/health retention. Enable it only after event outcomes have been observed in live operation.
