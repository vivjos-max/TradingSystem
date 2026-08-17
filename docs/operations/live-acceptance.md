# Live acceptance

This is the release gate for the read-only Groww foundation. It contacts the subscribed account and a running five-service stack. Fixtures, sample values, and a skipped live run do not pass the gate.

## Preconditions

- Docker Desktop and Docker Compose are running.
- A private env file exists outside the repository and every OneDrive-synced directory.
- The env file contains non-empty `GROWW_TOTP_TOKEN`, `GROWW_TOTP_SECRET`, `POSTGRES_PASSWORD`, and `REDIS_PASSWORD` values and grants no access to `Everyone`, `Authenticated Users`, or `BUILTIN\Users`.
- The Groww Trading API subscription and required market segments are active.
- `scripts\bootstrap.ps1` has created `.venv` from the pinned lock.

Never pass credential values as PowerShell arguments, pytest options, or chat text. The acceptance script accepts only the private file path and does not print its contents. Docker receives the named variables already declared in `compose.yaml`; the API receives no `GROWW_*` variable.

## Run

From the repository root:

```powershell
$envFile = Join-Path $env:USERPROFILE ".config\TradingSystem\live.env"
scripts\live-acceptance.ps1 -EnvFile $envFile
```

The script builds and starts exactly `frontend`, `api`, `worker`, `postgres`, and `redis`, then runs the credentialed pytest itself. Do not run pytest with `LIVE_ACCEPTANCE=1` unless the same five-service stack is already running.

For collection or development without credentials, use:

```powershell
.venv\Scripts\python.exe -m pytest apps\backend\tests\live\test_live_foundation.py -q -rs
```

That command visibly skips because `LIVE_ACCEPTANCE=1` is absent. A skip is not release evidence.

## Gates

The script fails if any of these checks fail:

- Compose is not exactly five services, the API has a `GROWW_*` environment name, or application source exposes an execution/private-SDK surface.
- A runtime image contains `.env`, tests, fixtures, or artifacts.
- `/run/groww-feed` is not worker-only tmpfs with mode `0700` and UID/GID `10001`, or feed credential files appear in the image/package path.
- Direct reads through `GrowwReadClient` do not reconcile with the running API for NIFTY spot, nearest future, ATM ±5 options, margin, positions, segmented orders, or fills.
- Market/account timestamps differ by more than 75 seconds, or values exceed the documented tolerance: 5 points absolute or 2% relative for changing market values and ₹0.01 for reconciled margin values.
- OpenAPI has a method other than GET/HEAD, an execution-shaped path/schema, or the live Python process loads a contract fixture module.
- During an open session, a stopped worker does not produce `STALE`, or restart does not reconcile the account before readiness.
- A stored 1m candle differs from independently queried durable observations. When a complete 5m window exists, its derived OHLC must also match those observations.

The market tolerances can be tightened for a controlled observation with `LIVE_TIMESTAMP_TOLERANCE_SECONDS`, `LIVE_PRICE_ABSOLUTE_TOLERANCE`, and `LIVE_VALUE_RELATIVE_TOLERANCE`. They exist only for REST/feed sampling skew; widening them is a release-gate change and requires review. `LIVE_CANDLE_WAIT_SECONDS` defaults to 90.

## Closed-market behavior

Outside an exchange session, direct REST/account/OpenAPI/container checks still run. Feed-stale, feed-readiness, and candle proof are recorded as `PENDING` with reason `MARKET_CLOSED`; they are not converted to pass. Repeat the gate during an exchange-open window before release.

Authentication, entitlement, pagination, contract-shape, reconciliation, container-boundary, and value mismatches are failures, never pending conditions.

## Evidence

The only persistent output is the ignored file `artifacts/live-acceptance/latest.json`. Its allowlist is limited to:

- SDK/application versions and UTC timestamps;
- NIFTY instrument symbols/tokens;
- option, position, order, and fill counts;
- observed state transitions;
- named `PASS`, `FAIL`, or `PENDING` results with safe reason codes.

The report must never contain credentials, environment dumps, full account/order payloads, prices, balances, P&L, order IDs, or feed credential contents. If unexpected sensitive data appears, remove the report immediately and treat the gate as failed.

PostgreSQL uses a durable named volume. Do not use `docker compose down -v` after live data exists.
