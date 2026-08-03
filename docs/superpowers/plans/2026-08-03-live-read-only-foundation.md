# Live Read-Only Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local, read-only application that authenticates to the user's subscribed Groww account, displays current NIFTY and account state, consumes the Groww feed, persists normalized observations, and proves that no execution endpoint exists.

**Architecture:** A Python service owns authentication, Groww reads, normalization, deterministic market logic, PostgreSQL persistence, Redis live state, and a streaming worker. FastAPI exposes only normalized `GET` resources to a Next.js dashboard. Live Groww failure always becomes an explicit operational state; test fixtures are never a runtime data source.

**Tech Stack:** Python 3.12, FastAPI 0.139.2, Pydantic 2.13.4, SQLAlchemy 2.0.51, PostgreSQL 17, Redis 8, `growwapi` 1.5.0, `pandas_market_calendars` 5.4.0, pytest 9.1.1, Next.js 16.2.12, React 19.2.8, TypeScript 5.9.3, Vitest 4.1.10, Testing Library, pnpm 11, Docker Compose.

## Global Constraints

- The runtime contains no order placement, modification, cancellation, GTT, OCO, or generic Groww SDK passthrough.
- The generated OpenAPI document contains only `GET` and `HEAD` operations; tests fail on any mutation operation or execution-shaped path.
- Runtime data is always live Groww data or an explicit `BLOCKED`, `STALE`, `DEGRADED`, or `MARKET_CLOSED` state. Fixtures exist under tests only.
- Credentials remain server-side, are loaded from an ignored `.env`, use Pydantic secret types, and are redacted from logs and API responses.
- Only the worker process receives Groww TOTP values. The frontend and API process receive no Groww credential or access-token environment variable.
- All persisted timestamps are timezone-aware UTC. Exchange-session decisions and display times use `Asia/Kolkata`.
- Instrument symbols, exchange tokens, expiry dates, strikes, lot sizes, and tick sizes come from Groww's instrument master.
- The selected option window is ATM plus five strikes on either side for the nearest eligible NIFTY expiry.
- PostgreSQL is the durable source; Redis contains replaceable current state only.
- The API binds to `127.0.0.1` by default.
- Python code targets Python 3.11 or later; this workspace will use the bundled Python 3.12.13 runtime.
- Next.js requires Node 20.9 or later; this workspace will use the bundled Node 24.14.0 runtime and pnpm 11.9.0.
- Docker is not currently installed on the host. Tasks not requiring Docker can proceed, but PostgreSQL/Redis integration and live Compose acceptance cannot be declared complete until Docker Desktop or a compatible Docker engine is available.
- Every behavior change follows test-driven development: write one focused test, run it and observe the expected failure, implement the minimum, then run the focused and affected suites.

## File Map

```text
TradingSystem/
|-- .env.example
|-- .gitignore
|-- README.md
|-- compose.yaml
|-- scripts/
|   |-- bootstrap.ps1
|   |-- test.ps1
|   `-- live-acceptance.ps1
|-- apps/
|   |-- backend/
|   |   |-- Dockerfile
|   |   |-- pyproject.toml
|   |   |-- requirements.lock
|   |   |-- alembic.ini
|   |   |-- migrations/env.py
|   |   |-- migrations/versions/0001_read_only_foundation.py
|   |   |-- src/trading_system/__init__.py
|   |   |-- src/trading_system/settings.py
|   |   |-- src/trading_system/domain/models.py
|   |   |-- src/trading_system/market/instruments.py
|   |   |-- src/trading_system/market/candles.py
|   |   |-- src/trading_system/market/session.py
|   |   |-- src/trading_system/market/freshness.py
|   |   |-- src/trading_system/brokers/ports.py
|   |   |-- src/trading_system/brokers/groww/auth.py
|   |   |-- src/trading_system/brokers/groww/client.py
|   |   |-- src/trading_system/brokers/groww/normalizers.py
|   |   |-- src/trading_system/persistence/models.py
|   |   |-- src/trading_system/persistence/postgres.py
|   |   |-- src/trading_system/persistence/redis_state.py
|   |   |-- src/trading_system/services/readiness.py
|   |   |-- src/trading_system/services/snapshot.py
|   |   |-- src/trading_system/worker/feed.py
|   |   |-- src/trading_system/worker/main.py
|   |   |-- src/trading_system/api/dependencies.py
|   |   |-- src/trading_system/api/main.py
|   |   |-- src/trading_system/api/routes/health.py
|   |   |-- src/trading_system/api/routes/system.py
|   |   |-- src/trading_system/api/routes/market.py
|   |   |-- src/trading_system/api/routes/account.py
|   |   |-- tests/unit/
|   |   |-- tests/contract/
|   |   |-- tests/integration/
|   |   `-- tests/live/
|   `-- frontend/
|       |-- Dockerfile
|       |-- package.json
|       |-- pnpm-lock.yaml
|       |-- next.config.ts
|       |-- tsconfig.json
|       |-- vitest.config.ts
|       |-- src/app/layout.tsx
|       |-- src/app/page.tsx
|       |-- src/app/globals.css
|       |-- src/components/GlobalStatus.tsx
|       |-- src/components/ServiceGrid.tsx
|       |-- src/components/MarketPanel.tsx
|       |-- src/components/OptionTable.tsx
|       |-- src/components/CandlePanel.tsx
|       |-- src/components/AccountPanel.tsx
|       |-- src/lib/api.ts
|       |-- src/lib/types.ts
|       |-- src/lib/format.ts
|       |-- src/tests/page.test.tsx
|       |-- src/tests/GlobalStatus.test.tsx
|       `-- src/tests/OptionTable.test.tsx
`-- docs/
    |-- operations/live-acceptance.md
    `-- superpowers/specs and superpowers/plans
```

---

### Task 1: Reproducible workspace and secret-safe settings

**Files:**
- Create: `.gitignore`
- Create: `.env.example`
- Create: `scripts/bootstrap.ps1`
- Create: `apps/backend/pyproject.toml`
- Create: `apps/backend/requirements.lock`
- Create: `apps/backend/src/trading_system/__init__.py`
- Create: `apps/backend/src/trading_system/settings.py`
- Test: `apps/backend/tests/unit/test_settings.py`

**Interfaces:**
- Produces: `Settings`, `get_settings()`, `SafeSettingsView`, and a workspace `.venv` using Python 3.12.
- Security contract: `repr(settings)`, validation errors, logs, and `SafeSettingsView` never reveal `groww_totp_token`, `groww_totp_secret`, database password, or Redis password.

- [ ] **Step 1: Create package metadata and the failing settings test**

Define Python 3.11+ metadata and a `src` package. Pin runtime dependencies exactly to `fastapi==0.139.2`, `pydantic==2.13.4`, `pydantic-settings==2.14.2`, `growwapi==1.5.0`, `sqlalchemy==2.0.51`, `alembic==1.18.5`, `asyncpg==0.31.0`, `redis==8.0.1`, `pyotp==2.10.0`, `pandas==3.0.3`, `pandas-market-calendars==5.4.0`, `uvicorn[standard]==0.51.0`, `httpx==0.28.1`, and `structlog==26.1.0`. Pin development dependencies to `pip-tools==7.6.0`, `pytest==9.1.1`, `pytest-asyncio==1.4.0`, `pytest-cov==7.1.0`, `testcontainers==4.14.2`, `PyYAML==6.0.3`, `ruff==0.15.22`, and `mypy==2.3.0`. Generate `requirements.lock` with hashes using `pip-compile --generate-hashes` and install only from that lock in Docker.

Write tests that construct settings with literal test secrets and assert redaction:

```python
def test_safe_view_never_contains_groww_secrets(monkeypatch):
    monkeypatch.setenv("GROWW_TOTP_TOKEN", "test-token-sensitive")
    monkeypatch.setenv("GROWW_TOTP_SECRET", "test-secret-sensitive")
    settings = Settings(_env_file=None)
    rendered = settings.safe_view().model_dump_json()
    assert "test-token-sensitive" not in rendered
    assert "test-secret-sensitive" not in rendered
    assert settings.safe_view().groww_credentials_configured is True
```

- [ ] **Step 2: Bootstrap the environment, then confirm the missing module failure**

Run:

```powershell
scripts\bootstrap.ps1
.venv\Scripts\python.exe -m pytest apps/backend/tests/unit/test_settings.py -q
```

Expected: FAIL because `trading_system.settings` does not exist.

- [ ] **Step 3: Implement settings and repository secret rules**

Use `SecretStr` for credential fields, `SettingsConfigDict(env_file=".env", extra="forbid")`, and these non-secret defaults: API host `127.0.0.1`, API port `8000`, market timezone `Asia/Kolkata`, feed stale seconds `10`, quote stale seconds `15`, account stale seconds `60`, dependency timeout seconds `2`, and option strikes each side `5`.

`.env.example` defines `GROWW_TOTP_TOKEN`, `GROWW_TOTP_SECRET`, `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, and `NEXT_PUBLIC_API_BASE_URL`. Groww values and database/cache passwords are blank; hosts, ports, database/user names, and the localhost API URL have safe development defaults. `Settings` constructs database and Redis URLs internally so passwords never appear in process listings. `.gitignore` excludes `.env`, `.venv`, Python caches, coverage output, `.next`, `node_modules`, and live acceptance reports.

- [ ] **Step 4: Run focused verification**

Run:

```powershell
.venv\Scripts\python.exe -m pytest apps/backend/tests/unit/test_settings.py -q
.venv\Scripts\python.exe -m ruff check apps/backend/src apps/backend/tests
```

Expected: both exit 0.

- [ ] **Step 5: Commit the independently reviewable settings boundary**

```powershell
git add .gitignore .env.example scripts/bootstrap.ps1 apps/backend
git commit -m "build: bootstrap secret-safe backend"
```

---

### Task 2: Normalized domain models and NIFTY instrument selection

**Files:**
- Create: `apps/backend/src/trading_system/domain/models.py`
- Create: `apps/backend/src/trading_system/market/instruments.py`
- Test: `apps/backend/tests/unit/test_instruments.py`

**Interfaces:**
- Produces immutable Pydantic models: `Instrument`, `Quote`, `OptionContract`, `OptionChain`, `MarginSummary`, `Position`, `Order`, `Fill`, `MarketObservation`, `Candle`, `ServiceHealth`, and `SystemStatus`.
- Produces `resolve_nifty_universe(instruments, spot, as_of) -> NiftyUniverse`.
- `NiftyUniverse` contains the NIFTY index, nearest non-expired future, nearest non-expired option expiry, ATM strike, and the eleven selected strikes.

- [ ] **Step 1: Write failing selection tests**

Cover expiry rollover, strike rounding from the instrument master's actual strike ladder, exclusion of expired/reserved/non-buyable contracts, and lot/tick preservation.

```python
def test_resolve_nifty_universe_uses_actual_strike_ladder(instruments):
    universe = resolve_nifty_universe(
        instruments=instruments,
        spot=Decimal("24776.40"),
        as_of=datetime(2026, 8, 3, 4, 15, tzinfo=UTC),
    )
    assert universe.atm_strike == Decimal("24800")
    assert universe.selected_strikes == tuple(Decimal(v) for v in range(24550, 25051, 50))
    assert universe.option_expiry.isoformat() == "2026-08-06"
```

- [ ] **Step 2: Run the focused test and confirm missing model/function failures**

Run: `.venv\Scripts\python.exe -m pytest apps/backend/tests/unit/test_instruments.py -q`

Expected: FAIL because the domain models and resolver do not exist.

- [ ] **Step 3: Implement normalized models and deterministic selection**

Use `Decimal` for monetary/price fields, aware `datetime` values, enums for source and health state, and `ConfigDict(frozen=True)`. Determine the strike ladder from unique eligible strikes; select the nearest strike using absolute distance with the lower strike as the deterministic tie-breaker. Select expiry/future by earliest expiry on or after the exchange date.

- [ ] **Step 4: Run unit and type checks**

Run:

```powershell
.venv\Scripts\python.exe -m pytest apps/backend/tests/unit/test_instruments.py -q
.venv\Scripts\python.exe -m mypy apps/backend/src/trading_system/domain apps/backend/src/trading_system/market/instruments.py
```

Expected: both exit 0.

- [ ] **Step 5: Commit the domain boundary**

```powershell
git add apps/backend/src/trading_system/domain apps/backend/src/trading_system/market apps/backend/tests/unit/test_instruments.py
git commit -m "feat: add normalized NIFTY instrument domain"
```

---

### Task 3: Session calendar, candle construction, and freshness states

**Files:**
- Create: `apps/backend/src/trading_system/market/session.py`
- Create: `apps/backend/src/trading_system/market/candles.py`
- Create: `apps/backend/src/trading_system/market/freshness.py`
- Test: `apps/backend/tests/unit/test_session.py`
- Test: `apps/backend/tests/unit/test_candles.py`
- Test: `apps/backend/tests/unit/test_freshness.py`

**Interfaces:**
- Produces `NseSessionCalendar.state_at(instant) -> SessionState` using the `NSE` calendar from `pandas_market_calendars`.
- Produces `CandleBuilder.accept(observation) -> CandleUpdate` and `aggregate_five_minute(candles) -> Candle`.
- Produces `derive_system_status(session, services, now, thresholds) -> SystemStatus`.

- [ ] **Step 1: Write failing tests for exchange-state decisions**

Test a regular open minute, weekend, the 2026 Independence Day weekend, a documented 2026 NSE holiday, and an unknown calendar horizon. The unknown-horizon case must return `BLOCKED` rather than guessing.

- [ ] **Step 2: Run the session tests and observe the missing implementation failure**

Run: `.venv\Scripts\python.exe -m pytest apps/backend/tests/unit/test_session.py -q`

Expected: FAIL because `NseSessionCalendar` does not exist.

- [ ] **Step 3: Implement the calendar wrapper and explicit coverage horizon**

Wrap the dependency behind `NseSessionCalendar`, verify the selected dependency includes the requested date, expose market-open and close instants in UTC, and return calendar-unknown beyond the tested 2026-2027 horizon. Do not add an NSE webpage scraper.

- [ ] **Step 4: Write and fail focused candle tests**

```python
def test_late_observation_is_audited_without_rewriting_final_candle(builder):
    builder.accept(observation("2026-08-03T03:45:05Z", "100"))
    finalized = builder.finalize_through(datetime.fromisoformat("2026-08-03T03:46:00+00:00"))
    late = builder.accept(observation("2026-08-03T03:45:30Z", "120"))
    assert late.late is True
    assert finalized[0].high == Decimal("100")
```

Also test half-open minute boundaries, event-time ordering, unavailable volume, no cross-session aggregation, and exact five-minute aggregation from five finalized one-minute candles.

- [ ] **Step 5: Implement and verify candle behavior**

Run: `.venv\Scripts\python.exe -m pytest apps/backend/tests/unit/test_candles.py -q`

Expected after implementation: PASS.

- [ ] **Step 6: Write, fail, and implement freshness precedence tests**

Assert `BLOCKED > STALE > DEGRADED > MARKET_CLOSED > LIVE`, and assert market-closed data is labelled with its last timestamp rather than stale. Inject `now`; never call the wall clock inside decision functions.

Run: `.venv\Scripts\python.exe -m pytest apps/backend/tests/unit/test_freshness.py -q`

Expected after implementation: PASS.

- [ ] **Step 7: Run the complete market suite and commit**

```powershell
.venv\Scripts\python.exe -m pytest apps/backend/tests/unit/test_session.py apps/backend/tests/unit/test_candles.py apps/backend/tests/unit/test_freshness.py -q
git add apps/backend/src/trading_system/market apps/backend/tests/unit
git commit -m "feat: add deterministic sessions candles and freshness"
```

---

### Task 4: Groww authentication, read port, and contract normalization

**Files:**
- Create: `apps/backend/src/trading_system/brokers/ports.py`
- Create: `apps/backend/src/trading_system/brokers/groww/auth.py`
- Create: `apps/backend/src/trading_system/brokers/groww/client.py`
- Create: `apps/backend/src/trading_system/brokers/groww/normalizers.py`
- Create: `apps/backend/tests/contract/fixtures/quote.json`
- Create: `apps/backend/tests/contract/fixtures/option_chain.json`
- Create: `apps/backend/tests/contract/fixtures/greeks.json`
- Create: `apps/backend/tests/contract/fixtures/margin.json`
- Create: `apps/backend/tests/contract/fixtures/positions.json`
- Create: `apps/backend/tests/contract/fixtures/orders.json`
- Create: `apps/backend/tests/contract/fixtures/trades.json`
- Create: `apps/backend/tests/contract/fixtures/instruments.json`
- Create: `apps/backend/tests/contract/test_groww_contract.py`
- Create: `apps/backend/tests/unit/test_groww_auth.py`
- Create: `apps/backend/tests/unit/test_groww_read_client.py`

**Interfaces:**
- Produces `BrokerReadPort` with exactly: `instruments()`, `nifty_quote()`, `option_chain(expiry)`, `greeks(contracts)`, `margin()`, `positions()`, `orders()`, and `fills(orders)`.
- Produces `GrowwAuthenticator.create_client(settings) -> GrowwAPI` using `pyotp.TOTP(secret).now()` and `GrowwAPI.get_access_token(api_key=token, totp=code)`.
- Produces `GrowwReadClient(BrokerReadPort)` with no `__getattr__`, SDK escape hatch, or mutation method.

- [ ] **Step 1: Write the failing authentication secrecy test**

Inject factories for TOTP and the SDK static token method. Assert the correct token/code flow and assert raised `BrokerUnavailable` messages omit both secrets.

- [ ] **Step 2: Run the auth test and confirm the missing class failure**

Run: `.venv\Scripts\python.exe -m pytest apps/backend/tests/unit/test_groww_auth.py -q`

Expected: FAIL because `GrowwAuthenticator` does not exist.

- [ ] **Step 3: Implement the auth adapter and safe exception mapping**

Map SDK authentication, rate-limit, timeout, permission, and malformed-response failures to stable codes: `GROWW_AUTH_FAILED`, `GROWW_RATE_LIMITED`, `GROWW_TIMEOUT`, `GROWW_PERMISSION_DENIED`, and `GROWW_CONTRACT_MISMATCH`. Preserve a correlation ID and safe summary.

- [ ] **Step 4: Write contract fixtures and failing normalization tests**

Fixtures reproduce only documented Groww response shapes and contain invented account values. Validate `get_all_instruments`, `get_quote`, `get_option_chain`, `get_greeks`, `get_available_margin_details`, `get_positions_for_user`, `get_order_list`, and `get_trade_list_for_order` mappings. Missing required fields must fail with `GROWW_CONTRACT_MISMATCH`; documented optional values become `None`.

- [ ] **Step 5: Implement normalizers and the explicit read client**

Use only the documented SDK methods. Page orders with `page_size=25` until the returned page is shorter than 25. Fetch fills only for returned orders with `page_size=50`, and deduplicate by Groww trade ID or exchange trade ID. Stamp every normalized result with source `GROWW_LIVE` and the broker timestamp where supplied.

- [ ] **Step 6: Prove the adapter exposes no mutation surface**

Add an interface test:

```python
def test_read_port_has_no_execution_methods():
    forbidden = {"place_order", "modify_order", "cancel_order", "create_smart_order"}
    public_methods = {name for name in dir(GrowwReadClient) if not name.startswith("_")}
    assert forbidden.isdisjoint(public_methods)
```

- [ ] **Step 7: Run contract, unit, lint, and type checks; commit**

```powershell
.venv\Scripts\python.exe -m pytest apps/backend/tests/contract apps/backend/tests/unit/test_groww_auth.py apps/backend/tests/unit/test_groww_read_client.py -q
.venv\Scripts\python.exe -m ruff check apps/backend/src/trading_system/brokers apps/backend/tests/contract
.venv\Scripts\python.exe -m mypy apps/backend/src/trading_system/brokers
git add apps/backend/src/trading_system/brokers apps/backend/tests/contract apps/backend/tests/unit/test_groww_auth.py apps/backend/tests/unit/test_groww_read_client.py
git commit -m "feat: add read-only Groww contract adapter"
```

---

### Task 5: Durable PostgreSQL repository and replaceable Redis state

**Files:**
- Create: `apps/backend/alembic.ini`
- Create: `apps/backend/migrations/env.py`
- Create: `apps/backend/migrations/versions/0001_read_only_foundation.py`
- Create: `apps/backend/src/trading_system/persistence/models.py`
- Create: `apps/backend/src/trading_system/persistence/postgres.py`
- Create: `apps/backend/src/trading_system/persistence/redis_state.py`
- Test: `apps/backend/tests/integration/test_postgres_repository.py`
- Test: `apps/backend/tests/integration/test_redis_state.py`

**Interfaces:**
- Produces `PostgresRepository` methods `save_instrument_import`, `save_broker_snapshot`, `save_observation`, `save_candles`, `save_account_snapshot`, `save_health_transition`, and read methods needed by the API.
- Produces `RedisLiveState` methods `set_quote`, `set_option_chain`, `set_in_progress_candle`, `set_heartbeat`, `set_account_summary`, `set_subscription_state`, and corresponding reads.
- Every write accepts a deterministic idempotency key.

- [ ] **Step 1: Write failing PostgreSQL idempotency tests**

Apply the migration to a disposable PostgreSQL database, insert the same observation twice, and assert one row. Insert a late observation and assert its audit flag. Verify UTC timestamps and JSONB raw payload retention.

- [ ] **Step 2: Run the integration test and confirm failure before schema/repository exist**

Run: `.venv\Scripts\python.exe -m pytest apps/backend/tests/integration/test_postgres_repository.py -q -m integration`

Expected: FAIL because migration/repository modules do not exist. If Docker is unavailable, record the environmental blocker and continue only with non-integration tasks; do not mark this task complete.

- [ ] **Step 3: Implement schema and repository**

Create tables `instrument_imports`, `broker_snapshots`, `market_observations`, `candles`, `account_snapshots`, and `health_transitions`. Add unique constraints on source/idempotency key and candle `(instrument_key, interval, opened_at, source)`. Use SQLAlchemy async sessions and PostgreSQL `ON CONFLICT DO NOTHING` or deterministic upsert where current-state replacement is intended.

- [ ] **Step 4: Write failing Redis recovery tests**

Assert key TTLs, JSON schema round-trips, heartbeat timestamps, and that `rebuild_from_repository()` restores latest replaceable state after `FLUSHDB` in the disposable test Redis instance.

- [ ] **Step 5: Implement Redis state with namespaced keys**

Prefix every key with `trading-system:v1:`. Never store secrets, SDK clients, or access tokens. Use explicit TTLs: heartbeat 30 seconds, in-progress candle 10 minutes, quote/option state 1 hour, account summary 5 minutes.

- [ ] **Step 6: Run integration verification and commit**

```powershell
.venv\Scripts\python.exe -m pytest apps/backend/tests/integration/test_postgres_repository.py apps/backend/tests/integration/test_redis_state.py -q -m integration
.venv\Scripts\python.exe -m alembic -c apps/backend/alembic.ini upgrade head
git add apps/backend/alembic.ini apps/backend/migrations apps/backend/src/trading_system/persistence apps/backend/tests/integration
git commit -m "feat: persist read-only market and account state"
```

---

### Task 6: Feed worker, startup reconciliation, and reconnect state machine

**Files:**
- Create: `apps/backend/src/trading_system/services/readiness.py`
- Create: `apps/backend/src/trading_system/services/snapshot.py`
- Create: `apps/backend/src/trading_system/worker/feed.py`
- Create: `apps/backend/src/trading_system/worker/main.py`
- Test: `apps/backend/tests/unit/test_readiness.py`
- Test: `apps/backend/tests/unit/test_snapshot_service.py`
- Test: `apps/backend/tests/unit/test_feed_worker.py`

**Interfaces:**
- Produces `ReadinessCoordinator(redis_state)` that persists transitions in Redis and becomes ready only after dependencies, reconciliation, and the first valid live update succeed; the separate API process reads this shared state.
- Produces `SnapshotService.refresh() -> SnapshotBundle` for instruments, market, option, margin, positions, orders, and fills.
- Produces `GrowwFeedWorker.run()` and `stop()` around `GrowwFeed`, normalized callbacks, bounded exponential reconnect, candle updates, PostgreSQL writes, and Redis heartbeats.

- [ ] **Step 1: Write the failing readiness ordering test**

```python
async def test_readiness_requires_reconciliation_before_first_tick(coordinator):
    await coordinator.dependencies_ready()
    await coordinator.first_live_update()
    assert coordinator.is_ready is False
    await coordinator.reconciled()
    assert coordinator.is_ready is True
```

Also assert any blocked dependency resets readiness and startup never assumes an empty position list without a successful broker response.

- [ ] **Step 2: Run and observe the missing coordinator failure**

Run: `.venv\Scripts\python.exe -m pytest apps/backend/tests/unit/test_readiness.py -q`

Expected: FAIL because the coordinator is missing.

- [ ] **Step 3: Implement readiness and snapshot orchestration**

Perform startup in the approved order: settings, PostgreSQL, Redis, Groww auth, instruments, account reconciliation, initial snapshots, subscriptions, first live update. Retry safe reads with delays `1, 2, 4, 8, 16, 30` seconds plus bounded jitter; reset the series after a successful refresh.

- [ ] **Step 4: Write failing feed callback and duplicate tests**

Use a minimal injected feed protocol, not the concrete SDK. Test index callbacks using `value`, derivative callbacks using `ltp`, `tsInMillis` conversion, duplicate idempotency keys, heartbeat refresh, stale transition after callback silence, and stop/unsubscribe behavior.

- [ ] **Step 5: Implement the feed worker**

Subscribe to the NIFTY index via `subscribe_index_value` and selected future/options via `subscribe_ltp`. The callback reads `get_index_value()` or `get_ltp()`, normalizes observations, updates Redis, persists raw/normalized observations, and forwards them to the candle builder. The blocking `consume()` call runs in the worker process, never inside the FastAPI event loop.

- [ ] **Step 6: Run worker suites and commit**

```powershell
.venv\Scripts\python.exe -m pytest apps/backend/tests/unit/test_readiness.py apps/backend/tests/unit/test_snapshot_service.py apps/backend/tests/unit/test_feed_worker.py -q
git add apps/backend/src/trading_system/services apps/backend/src/trading_system/worker apps/backend/tests/unit
git commit -m "feat: add live feed and startup reconciliation"
```

---

### Task 7: Read-only FastAPI surface and OpenAPI execution guard

**Files:**
- Create: `apps/backend/src/trading_system/api/dependencies.py`
- Create: `apps/backend/src/trading_system/api/main.py`
- Create: `apps/backend/src/trading_system/api/routes/health.py`
- Create: `apps/backend/src/trading_system/api/routes/system.py`
- Create: `apps/backend/src/trading_system/api/routes/market.py`
- Create: `apps/backend/src/trading_system/api/routes/account.py`
- Test: `apps/backend/tests/unit/test_api.py`
- Test: `apps/backend/tests/unit/test_openapi_read_only.py`

**Interfaces:**
- Produces `create_app(container: AppContainer) -> FastAPI`.
- Exposes the eight approved endpoints from the design spec.
- Returns safe correlation IDs and timestamps; never returns settings, credentials, tokens, raw SDK clients, or full raw broker payloads.

- [ ] **Step 1: Write failing endpoint-state tests**

Test `/health/live` returns process liveness even when dependencies are blocked, `/health/ready` returns HTTP 503 until the coordinator is ready, and market/account endpoints return normalized response models with explicit source timestamps.

- [ ] **Step 2: Run and observe missing app failure**

Run: `.venv\Scripts\python.exe -m pytest apps/backend/tests/unit/test_api.py -q`

Expected: FAIL because `create_app` and routes do not exist.

- [ ] **Step 3: Implement the read endpoints and safe dependency injection**

Use repository/live-state ports injected through `AppContainer`. Set CORS to only `http://127.0.0.1:3000` and `http://localhost:3000`. Return 503 with a structured safe error when required live state is unavailable; never substitute fixture data.

- [ ] **Step 4: Write the OpenAPI guard before running it**

```python
def test_openapi_contains_no_mutation_or_execution_surface(app):
    schema = app.openapi()
    allowed = {"get", "head", "parameters"}
    for path, path_item in schema["paths"].items():
        assert set(path_item).issubset(allowed), path
        assert not any(word in path.lower() for word in ("place", "modify", "cancel", "order-ticket", "execute"))
    serialized = json.dumps(schema).lower()
    assert "placeorder" not in serialized
    assert "createorder" not in serialized
```

- [ ] **Step 5: Run API, OpenAPI, and full backend unit tests; commit**

```powershell
.venv\Scripts\python.exe -m pytest apps/backend/tests/unit/test_api.py apps/backend/tests/unit/test_openapi_read_only.py -q
.venv\Scripts\python.exe -m pytest apps/backend/tests/unit apps/backend/tests/contract -q
git add apps/backend/src/trading_system/api apps/backend/tests/unit/test_api.py apps/backend/tests/unit/test_openapi_read_only.py
git commit -m "feat: expose read-only system dashboard API"
```

---

### Task 8: Operational Next.js dashboard

**Files:**
- Create: `apps/frontend/package.json`
- Create: `apps/frontend/pnpm-lock.yaml`
- Create: `apps/frontend/next.config.ts`
- Create: `apps/frontend/tsconfig.json`
- Create: `apps/frontend/vitest.config.ts`
- Create: `apps/frontend/src/app/layout.tsx`
- Create: `apps/frontend/src/app/page.tsx`
- Create: `apps/frontend/src/app/globals.css`
- Create: `apps/frontend/src/components/GlobalStatus.tsx`
- Create: `apps/frontend/src/components/ServiceGrid.tsx`
- Create: `apps/frontend/src/components/MarketPanel.tsx`
- Create: `apps/frontend/src/components/OptionTable.tsx`
- Create: `apps/frontend/src/components/CandlePanel.tsx`
- Create: `apps/frontend/src/components/AccountPanel.tsx`
- Create: `apps/frontend/src/lib/api.ts`
- Create: `apps/frontend/src/lib/types.ts`
- Create: `apps/frontend/src/lib/format.ts`
- Test: `apps/frontend/src/tests/page.test.tsx`
- Test: `apps/frontend/src/tests/GlobalStatus.test.tsx`
- Test: `apps/frontend/src/tests/OptionTable.test.tsx`

**Interfaces:**
- Consumes: the approved FastAPI response schemas only.
- Produces: a localhost dashboard polling system status every 2 seconds, market data every 3 seconds, and account state every 15 seconds.
- UI safety contract: no button or form is labelled or wired as approve, buy, sell, exit, execute, or order.

- [ ] **Step 1: Create frontend package configuration and failing component tests**

Pin `next==16.2.12`, `react==19.2.8`, `react-dom==19.2.8`, `typescript==5.9.3`, `vitest==4.1.10`, `eslint==9.39.1`, `eslint-config-next==16.2.12`, `jsdom==27.2.0`, `@testing-library/react==16.3.0`, `@testing-library/jest-dom==6.9.1`, `@types/node==24.10.1`, `@types/react==19.2.5`, and `@types/react-dom==19.2.3`. Commit `pnpm-lock.yaml` so every direct and transitive frontend dependency is reproducible.

Test status precedence and stale timestamp visibility:

```tsx
it("renders blocked reason more prominently than cached prices", () => {
  render(<GlobalStatus status="BLOCKED" reason="Groww subscription unavailable" asOf="2026-08-03T04:30:00Z" />);
  expect(screen.getByRole("status")).toHaveTextContent("BLOCKED");
  expect(screen.getByText(/subscription unavailable/i)).toBeVisible();
});
```

- [ ] **Step 2: Run the tests and confirm missing component failures**

Run: `pnpm --dir apps/frontend test`

Expected: FAIL because the page/components do not exist.

- [ ] **Step 3: Implement typed API access and operational components**

Use server-safe `NEXT_PUBLIC_API_BASE_URL` defaulting to `http://127.0.0.1:8000`. All numbers include source timestamps. Option rows display strike, CE/PE LTP, bid, ask, spread, volume, OI, IV, and Greeks with an em dash for genuinely unavailable fields. Never compute missing broker values in the browser.

- [ ] **Step 4: Implement responsive visual hierarchy**

Use a restrained dark operational palette, tabular numerals, high-contrast status banners, compact desktop tables, and stacked mobile cards. `BLOCKED` and `STALE` states remain visually dominant. Use CSS only; defer charting libraries and render compact candle tables/sparklines from supplied OHLC values.

- [ ] **Step 5: Add a static safety test for prohibited controls**

Render the page and assert no interactive element has a prohibited execution label. Also scan the frontend source for `/orders/place`, `/execute`, and mutation fetch methods.

- [ ] **Step 6: Run frontend tests, lint, and production build; commit**

```powershell
pnpm --dir apps/frontend test
pnpm --dir apps/frontend lint
pnpm --dir apps/frontend build
git add apps/frontend
git commit -m "feat: add live read-only operations dashboard"
```

---

### Task 9: Compose topology, startup commands, and failure-safe local operation

**Files:**
- Create: `compose.yaml`
- Create: `apps/backend/Dockerfile`
- Create: `apps/frontend/Dockerfile`
- Create: `scripts/test.ps1`
- Modify: `scripts/bootstrap.ps1`
- Create: `README.md`
- Test: `apps/backend/tests/integration/test_compose_readiness.py`

**Interfaces:**
- Produces services `frontend`, `api`, `worker`, `postgres`, and `redis`.
- `api` and `worker` use the same backend image but different commands.
- Only frontend port 3000 and API port 8000 bind to localhost; PostgreSQL and Redis are not published to the LAN.

- [ ] **Step 1: Write the failing Compose configuration test**

Parse `compose.yaml` and assert the exact five services, localhost bindings, health checks, no privileged mode, no host network, read-only secrets via environment, and no service named executor or trader.

- [ ] **Step 2: Run and observe missing Compose failure**

Run: `.venv\Scripts\python.exe -m pytest apps/backend/tests/integration/test_compose_readiness.py -q`

Expected: FAIL because `compose.yaml` does not exist.

- [ ] **Step 3: Implement container images and Compose health ordering**

Use `python:3.12-slim`, `node:24-alpine`, `postgres:17-alpine`, and `redis:8-alpine`. Apply migrations before API startup. Pass Groww TOTP variables only to `worker`; pass only database, Redis, host, and CORS settings to `api`. Make the worker wait for healthy PostgreSQL and Redis but let Groww failures surface through application readiness rather than restart loops. Recreate the in-memory Groww access token on authentication expiry or feed reconnect.

- [ ] **Step 4: Implement developer scripts and operating guide**

`bootstrap.ps1` creates `.venv` with Python 3.12, installs the backend and frontend lockfiles, and refuses Python below 3.11 or Node below 20.9. `test.ps1` runs backend unit/contract tests and frontend tests/lint/build; with `-Integration`, it also runs Docker-backed tests.

README steps are exact: purchase API subscription, create TOTP credentials, copy `.env.example` to `.env`, fill only local secrets, start Compose, check `/health/ready`, open the dashboard, stop Compose, and troubleshoot each stable error code.

- [ ] **Step 5: Validate topology and restart behavior**

Run when Docker is available:

```powershell
docker compose config --quiet
docker compose up -d --build
docker compose ps
.venv\Scripts\python.exe -m pytest apps/backend/tests/integration -q -m integration
docker compose restart api worker
.venv\Scripts\python.exe -m pytest apps/backend/tests/integration/test_compose_readiness.py -q
```

Expected: Compose is valid; all infrastructure services become healthy; application readiness remains false until reconciliation and the first live update.

- [ ] **Step 6: Commit operational packaging**

```powershell
git add compose.yaml apps/backend/Dockerfile apps/frontend/Dockerfile scripts README.md apps/backend/tests/integration/test_compose_readiness.py
git commit -m "build: package read-only dashboard stack"
```

---

### Task 10: Credentialed live Groww acceptance and evidence report

**Files:**
- Create: `apps/backend/tests/live/test_groww_live.py`
- Create: `scripts/live-acceptance.ps1`
- Create: `docs/operations/live-acceptance.md`
- Runtime output, ignored: `artifacts/live-acceptance/latest.json`

**Interfaces:**
- Consumes local `.env` TOTP credentials and the running Compose stack.
- Produces a redacted acceptance report containing SDK version, timestamps, instrument identifiers, state transitions, counts, and pass/fail results; it excludes secrets and full account values.

- [ ] **Step 1: Write credential-gated live tests**

Mark tests `live` and skip with the explicit reason `Groww TOTP credentials are not configured` when secrets are absent. Tests call the real adapter and assert: authentication, NIFTY quote timestamp, nearest future, option expiry/window, broker-supplied chain fields, margin shape, position/order/fill reconciliation, and streaming heartbeat.

- [ ] **Step 2: Add live candle and failure-state assertions**

Collect accepted observations for at least one complete market minute during the regular session, reproduce its OHLC independently in the test, and compare it with the stored candle. Outside market hours, verify `MARKET_CLOSED` and historical timestamps instead. Stop the feed process and verify the system reports `STALE` during an open session; restart and verify reconciliation precedes readiness.

- [ ] **Step 3: Prove live runtime is not fixture-backed**

The acceptance script scans loaded module paths and application configuration, fails if any runtime path references `tests/contract/fixtures`, and records the installed `growwapi` version. It also fetches `/openapi.json` and reruns the no-mutation assertion against the running service.

- [ ] **Step 4: Run all automated verification before live access**

```powershell
scripts\test.ps1 -Integration
```

Expected: backend unit/contract/integration tests, frontend tests/lint/build, and Compose checks exit 0.

- [ ] **Step 5: Run the real Groww acceptance test**

Run during the relevant verification window:

```powershell
scripts\live-acceptance.ps1
```

Expected: authentication and all available live checks pass; market-hours-only checks either pass during the regular session or are explicitly recorded as pending with `MARKET_CLOSED`, never simulated.

- [ ] **Step 6: Review the redacted report and commit tests/docs only**

Confirm `artifacts/live-acceptance/latest.json` contains no token, secret, access token, full margin, or full position payload. Do not commit the artifact.

```powershell
git add apps/backend/tests/live/test_groww_live.py scripts/live-acceptance.ps1 docs/operations/live-acceptance.md
git commit -m "test: add credentialed Groww acceptance suite"
```

---

## Final Verification Gate

Run fresh, in this order:

```powershell
git diff --check
scripts\test.ps1 -Integration
scripts\live-acceptance.ps1
git status --short
```

Completion requires:

1. Backend unit and contract suites pass with zero failures.
2. PostgreSQL and Redis integration suites pass with zero failures.
3. Frontend tests, lint, type checking, and production build pass.
4. Compose configuration and health checks pass.
5. Live Groww authentication and read checks pass using the user's local TOTP credentials.
6. Market-hours-only candle/feed checks pass during an open session, or the release is reported as awaiting that specific observation; `MARKET_CLOSED` is not treated as proof of live streaming.
7. The running OpenAPI document contains no mutation operation or execution-shaped schema.
8. A source scan finds no order placement/modification/cancellation call in application code.
9. Logs, API responses, Git-tracked files, and the redacted acceptance report contain no credentials.
10. Restart reconciliation succeeds before the system reports ready.

Do not claim the foundation complete while Docker-backed verification, real Groww authentication, or the market-hours feed proof remains unavailable.
