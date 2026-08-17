# Phase 0 component reconciliation

The recovery implementation remains preserved at `codex/phase0-current-recovery-20260817` (`d99f56e`). The alternate implementation remains unchanged at `origin/codex/live-read-only-foundation` (`e8b9100`).

| Component | Recovery build | Remote foundation | Authoritative choice | Reason |
| --- | --- | --- | --- | --- |
| Configuration and secrets | Small Pydantic settings; scoped feed files; redacted persistence | More modular settings | Recovery | Already exercised with the live subscription and has the smaller public surface. Phase 0 adds only validated queue/freshness controls. |
| Groww authentication/adapter | SDK 1.5.0 gate, timeouts, segmented orders, paginated fills, feed-file cleanup | Broader modular ports but weaker fit to captured live contracts | Recovery plus contract fixes | Keeps the reviewed SDK surface and adds the read-only historical method. Live naive order timestamps are explicitly marked as assumed IST. |
| Instrument master | Live CSV normalization and NIFTY resolver | Separate instrument module | Recovery | Handles Groww's documented `IDX` value and the existing live schema. |
| Feed lifecycle | Previously reconnected inside one process | Also attempted generations in-process | New process boundary | Investigation proved public unsubscribe does not end `consume()`. One process now owns one immutable generation; death/silence exits non-zero for the supervisor. |
| Event validation | Generic `late` boolean | Better generation/idempotency patterns | Hybrid | Typed outcomes, event identity, three timestamps, and generation are added without replacing the working adapter. |
| Callback/persistence path | Synchronous per-event PostgreSQL connection | Better repository separation | Hybrid | Bounded standard-library queue, batched idempotent inserts, psycopg pool, and publish-after-commit. No distributed broker was added. |
| PostgreSQL schema | The schema holding the preserved live data | Incompatible initial Alembic schema | Recovery schema plus Alembic bridge | Avoids data translation or deletion. The native initial SQL remains unchanged and two forward-only revisions bridge and extend it. |
| Redis state | Proven live keys and explicit TTLs | Typed Redis repository | Recovery plus canonical option keys | Preserves compatible keys while separating option-field timestamps. |
| Candles | In-memory 1m/5m | Stronger event-time concepts | Hybrid | Adds session-aligned 15m, explicit completeness/provenance, durable reconstruction, and session pruning. No interpolation. |
| Historical data | Missing | Missing from the deployed path | New narrow adapter method | Uses Groww's documented read-only V2 candles and preserves `GROWW_HISTORICAL` provenance. |
| Freshness | Repeated endpoint checks | Pure shared derivation helper | Hybrid | One deterministic quality function now owns market timestamp ages/issues; existing operational thresholds stay configurable. |
| Account/orders/fills | Working pagination and startup reconciliation | More repository structure | Recovery | Preserves broker-returned values, labels realised P&L as broker supplied, and does not derive missing unrealised P&L/LTP. |
| API | Small GET-only FastAPI surface | Modular route tree | Recovery | The small allowlist is safer. It now exposes shared quality, 15m candles, provenance, and independently timed option fields. |
| Frontend | Existing dashboard | Alternate dashboard | Recovery | Preserves the accepted UI and corrects the browser URL to same-origin `/backend`; no redesign. |
| Compose/networking | Loopback ports, non-root worker, tmpfs, restart policy | Useful migration/quality patterns | Hybrid | Adds a one-shot migration service and keeps account/API access loopback-only. |
| Tests/CI | Live contract and safety tests | Broader lint/type/CI patterns | Hybrid | Keeps live fixtures and adds migration, lifecycle, queue, quality, reconstruction, historical, and mutation-boundary gates. |

## Authoritative Phase 0 boundary

```text
Groww read APIs/feed
  -> reviewed read-only adapter
  -> typed event validation
  -> bounded durable ingestion
  -> PostgreSQL evidence + Redis current state
  -> shared data quality
  -> GET-only API
```

No strategy, AI, recommendation, risk, paper-trading, journaling, or broker-mutation module is part of this branch.
