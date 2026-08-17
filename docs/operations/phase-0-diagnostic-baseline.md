# Phase 0 diagnostic baseline

Captured on 2026-08-17 before any Phase 0 migration or retention change.

## Recovery points

- Recovery branch: `codex/phase0-current-recovery-20260817`
- Recovery commit: `d99f56ef5932ef854596c6251b41c7e57a27d704`
- Preserved remote foundation: `origin/codex/live-read-only-foundation` at `e8b9100`
- Authoritative work branch: `codex/phase0-authoritative-foundation`

## Database evidence

- Private backup: `C:\Users\vivjo\.config\TradingSystem\backups\phase0-diagnostic-before-20260817T081658Z.dump`
- Backup SHA-256: `744F8385AFC470EEA2F2C9F80090C31EDF58E36923F026FA170209C7D68F51A8`
- Schema dump: `C:\Users\vivjo\.config\TradingSystem\backups\phase0-diagnostic-before-20260817T081658Z-schema.sql`
- Schema SHA-256: `A7CAA8765A9B180ED0D02BF57F080534E0CD8B30D8A8BE97010FDA0FDF434CED`
- Isolated restore: PostgreSQL 17.11 on `127.0.0.1:55432`; the active database was not changed.

| Table | Rows |
| --- | ---: |
| `events` | 102,279 |
| `candles` | 1,963 |
| `health_transitions` | 774 |

The restored clone matched the source schema, constraints, indexes, and all row counts.

## Legacy event-quality evidence

The old schema stores only a `late` boolean. It contains 76,000 flagged rows out of 102,279 events (74.31%). The typed Phase 0 migration must preserve these rows and backfill their outcome as `OTHER`; it must not guess a more specific cause retroactively.

| Kind | Total | Legacy flagged |
| --- | ---: | ---: |
| `account_snapshot` | 54 | 0 |
| `feed_depth` | 34,427 | 27,304 |
| `instrument_import` | 4 | 0 |
| `market_observation` | 67,616 | 48,696 |
| `market_snapshot` | 53 | 0 |
| `option_chain` | 53 | 0 |
| `subscription` | 72 | 0 |

## Feed investigation

Groww SDK 1.5.0 blocks `consume()` by joining its internal NATS consumer thread. Public unsubscribe removed subscriptions but did not stop that thread or return `consume()`. During the controlled reproduction the worker grew from 149 to 152 threads over 15 seconds while subscription records increased from 53 to 54. No production code used private SDK shutdown internals.

Conclusion: in-process feed replacement is unsafe. Phase 0 uses one feed generation per worker process and delegates recovery to the supervisor.

## Candle evidence

- NIFTY 1m: 75 rows, 12:29–13:45 IST observation window, one discontinuity (maximum three minutes).
- NIFTY 5m: 13 rows, 12:40–13:40 IST, no gap greater than five minutes.
- NIFTY future 1m: 50 rows with 17 discontinuities (maximum four minutes); only one 5m row.

The abnormal dataset is retained for diagnosis. Retention remains disabled until the typed event-quality model is deployed and verified.
