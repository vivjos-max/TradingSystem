# Phase 0 acceptance report

Observation date: 2026-08-17 (NSE live session). All times below are IST unless marked UTC.

## 1. Authoritative branch and commit

- Branch: `codex/phase0-authoritative-foundation`
- Recovery branch: `codex/phase0-current-recovery-20260817` remains unchanged at `d99f56e`.
- Alternate remote evidence remains unchanged at `origin/codex/live-read-only-foundation` (`e8b9100`).
- The exact authoritative commit is recorded in the delivery response because a commit cannot contain its own hash.

## 2. Component reconciliation decisions

The detailed decision table is in `docs/operations/phase-0-reconciliation.md`. The recovery adapter, GET-only API, account reconciliation, and accepted dashboard were preserved. Event validation, process-level feed lifecycle, durable ingestion, migrations, candle evidence, canonical option fields, same-origin routing, and quality gates were reconciled or added. Neither prior feed-reconnect implementation was retained.

## 3. Architecture after reconciliation

```text
Groww read APIs / one feed generation
  -> typed validation
  -> bounded queue
  -> idempotent pooled PostgreSQL batch
  -> publish accepted state to Redis only after commit
  -> shared deterministic data quality
  -> GET-only API
  -> same-origin private dashboard
```

Feed death, silence, persistence failure, or queue overflow blocks authority and exits the worker. A supervisor must create a new process and therefore a new SDK feed generation.

## 4. Files added, removed, and materially changed

- Added Alembic configuration and two revisions under `apps/backend/migrations/versions/`.
- Added `ingestion.py` and `quality.py`.
- Materially changed backend configuration, models, Groww adapter, market/candle code, store, worker, API, tests, locks, and container image.
- Materially changed the existing frontend page, build/lint configuration, same-origin routing, lockfile, and container image.
- Added CI plus Phase 0 baseline, reconciliation, and acceptance documentation.
- Changed Compose to migrate first, bind only to loopback, use same-origin API routing, and restart the worker on failure.
- Removed no preserved data or migration file. `migrations/001_initial.sql` remains unchanged.

## 5. Migrations

- `0001_current_foundation_baseline`: idempotent bridge over the preserved schema.
- `0002_phase0_event_quality`: typed event outcomes, three event timestamps, event identity, feed generation, rejection detail, 15m candles, and completeness/evidence columns.
- Both revisions are forward-only. Recovery is restore-from-backup, not destructive downgrade.
- The active local database was migrated only after clone verification and is at `0002_phase0_event_quality`.

## 6. Migration-clone verification

- Pre-migration custom-format backup: `C:\Users\vivjo\.config\TradingSystem\backups\phase0-diagnostic-before-20260817T081658Z.dump`
- Backup SHA-256: `744F8385AFC470EEA2F2C9F80090C31EDF58E36923F026FA170209C7D68F51A8`
- Schema SHA-256: `A7CAA8765A9B180ED0D02BF57F080534E0CD8B30D8A8BE97010FDA0FDF434CED`
- Isolated PostgreSQL 17 clone: `127.0.0.1:55432`.
- Before/after clone counts: events 102,279; candles 1,963; health transitions 774. Counts remained exact after test-artifact cleanup.
- Backfill: 26,279 `ACCEPTED`; 76,000 legacy flagged rows preserved as `OTHER` rather than guessed.
- A fresh empty database also upgraded to head successfully.
- Application integration tests passed against the migrated clone.
- `downgrade -1` failed deliberately without changing the head; restoring the verified dump is the tested recovery procedure.

## 7. Tests

- Full backend: 66 passed, 6 credential-gated live tests skipped, 0 failed.
- Real PostgreSQL/Redis integration passed against the migrated clone.
- Focused feed-lifecycle and read-only audit: 5 passed.
- Frontend status/same-origin tests: 3 passed.
- The controlled live-market observation and restart/reconstruction test described below also passed.

## 8. Lint, type, build, and dependency results

- Ruff check and format check: passed.
- mypy: passed for all 10 backend source modules.
- Python dependency check: no broken requirements.
- ESLint and TypeScript: passed.
- Next.js 16.3 production build: passed.
- pnpm audit: no known vulnerabilities.
- `git diff --check`: passed.

## 9. Feed lifecycle results

- Unit checks prove feed exception and open-session silence both unsubscribe, mark unhealthy, and raise for process exit.
- No code path constructs a replacement feed inside the same process.
- Controlled live generation 1 used one worker process and one generation for 21m 39.794s.
- Thread count was 37 at observation start and 34 at the end; it did not accumulate.
- One controlled process restart ended the old process and all of its threads. The new worker remained fail-closed during startup and reached `LIVE` in about 35 seconds with a new generation and 34 threads.
- Fault-driven restarts during the observation: 0. Controlled restarts: 1. Feed generations per process: 1.
- Compose declares `restart: on-failure`; local Compose execution was unavailable because Docker CLI is not installed.

## 10. Before/after live observations

### Before

- Preserved dataset capture duration: `UNKNOWN — NEEDS VERIFICATION`.
- Separate SDK lifecycle reproduction: 15 seconds; thread count 149 -> 152 while subscription records increased 53 -> 54.
- Feed generations were not instrumented in the old schema.
- Events: 102,279 total; 26,279 accepted; 76,000 generically flagged; rejection rate 74.31%.
- Rejection reasons and stale-incident count: `UNKNOWN — NEEDS VERIFICATION` because the old schema had only `late`.
- NIFTY 1m had one discontinuity (maximum three minutes); NIFTY 5m had no gap over five minutes; the future 1m series had 17 discontinuities (maximum four minutes).

### After

- Controlled generation: `8af802c1465b431daf11035c155473bd`, 14:25:17.681-14:46:57.475, 21m 39.794s.
- Events: 54,496 total; 53,655 accepted; 841 rejected; rejection rate 1.543%.
- Thread count: 37 start, 34 end. In-process feed reconnects: 0.
- Queue: size 0 at end; peak 41/4,096; overflow/dropped 0.
- Last measured PostgreSQL batch write: 0.526ms. Event receipt-to-processing average/max: 5.823/192.344ms.
- Forty-one 30-second API samples contained 8 transient fail-closed 503/STALE responses when the required future quote exceeded 15 seconds. Every sampled stale response recovered after a valid update; none retained false `LIVE`.
- Successful samples observed spot ages from -196ms to 39ms and future ages up to 11,131ms. Small negative ages remained within the configured 5-second source-clock allowance.
- NIFTY spot and August future produced continuous 14:30-14:45 windows: 15 complete 1m, three complete 5m, and one complete 15m candle each, with zero discontinuities.
- Same-origin `/backend` returned ready/LIVE after restart; browser-baked localhost is gone.

## 11. Event rejection breakdown

Generation 1:

| Outcome | Count |
| --- | ---: |
| `ACCEPTED` | 53,655 |
| `DUPLICATE` | 840 |
| `OUT_OF_ORDER` | 1 |
| all other rejection outcomes | 0 |

The second controlled process had its own generation (`cda1b547b9fb419b92ede5bb8763c809`), confirming the one-generation-per-process invariant. Typed but unobserved outcomes remain covered by unit tests.

## 12. Candle reconstruction

- The live 14:30-14:45 spot/future window contained 38 completed rows: 30 x 1m, 6 x 5m, and 2 x 15m.
- Restart rebuilt 38 `DURABLE_RECONSTRUCTED` counterparts from accepted durable observations.
- All 38 pairs matched exactly on OHLC, volume, completeness, expected/actual component counts, and finalization.
- PostgreSQL retains both provenance rows. The canonical read query selects one row per bucket with `LIVE_ACCEPTED` precedence; the same-origin API returned zero duplicate buckets.
- Missing lower-timeframe components produce `INCOMPLETE`, never a synthetic or falsely complete candle.

## 13. Historical-data provenance

- The read-only Groww historical adapter returns `GROWW_HISTORICAL` candles and normalizes the documented broker timestamps.
- Historical, live accepted, and durable reconstructed rows use distinct provenance and do not overwrite one another.
- No interpolation exists. No historical backfill was invoked during this live acceptance run, so live entitlement/response remains an unknown listed below.

## 14. Canonical option state

- The live endpoint returned 22 per-contract records.
- Each of LTP, bid, ask, depth, OI, IV, Greeks, volume, and option-chain snapshot has its own source and timestamp.
- One observed contract had LTP at 14:50:38.802, depth at 14:50:37.001, and OI/IV/volume/chain at 14:50:07.640, proving the API does not claim uniform freshness.
- Phase 0 performs no contract ranking or recommendation.

## 15. Storage and retention

- Existing abnormal diagnostic data and the verified backup were preserved.
- Retention remains disabled.
- Approved future policy is documented: depth current plus previous session, raw events five completed sessions, and Phase 0 candles/account/orders/fills/health retained.
- No retention job should be enabled until operational outcome distributions are reviewed over more sessions.

## 16. Remaining defects

- Current 15-second quote freshness produces transient `STALE` during sparse future updates. This is fail-safe and configurable; Phase 0 deliberately did not tune it.
- Docker/Compose could not be run locally because Docker CLI is unavailable; the supervisor contract is unit-tested, statically configured, and manually process-tested.
- FastAPI tests emit one upstream Starlette/httpx deprecation warning.
- Local Windows Redis requires RESP2; the application explicitly configures protocol 2.

## 17. Remaining unknowns

- Root causes within the legacy 76,000 generically flagged events cannot be recovered retroactively.
- Groww feed behavior over multi-hour/multi-session runs needs continued observation.
- Live historical-candle entitlement/response was not exercised; only the official SDK contract fixture was tested.
- NSE calendar coverage after 2026 requires a reviewed calendar update.
- Actual Compose restart behavior remains unobserved on this host until Docker is installed.

## 18. Broker-mutation boundary

Broker mutation remains impossible from application code. The adapter has no place/modify/cancel order or GTT/OCO method, source audit finds no mutation call, OpenAPI contains only the approved GET routes, and no AI/tool has broker access.

## 19. Recommendation

`READY FOR PHASE 1`

The authoritative data foundation, explicit quality model, reconstruction, canonical option state, process feed boundary, migration safety, same-origin private access, and read-only guard all passed. Phase 1 may add deterministic analytics only. It must not weaken the outstanding Docker/live-duration gates or introduce broker mutation.
