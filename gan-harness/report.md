# GAN evaluator report - iteration 3

## Verdict

**PASS - 7.6/10 (weighted 7.55; pass threshold 7.0).**

All code-only automatic-fail checks are clear. No critical or high code finding remains.

This is a code-only harness pass, not a full release-completion claim. Docker-backed integration/container checks and credentialed Groww market-hours acceptance remain explicit external blockers.

## Weighted score

| Criterion | Weight | Score | Weighted | Evidence |
|---|---:|---:|---:|---|
| Read-only security boundary | 0.30 | 8.0 | 2.400 | Secrets are typed/redacted and rejected from storage; API/UI are GET-only; no broker mutation call exists. Runtime containers are unverified. |
| Live correctness and failure honesty | 0.25 | 7.5 | 1.875 | Past/future required sources fail closed; future/stale LTP and depth events are audited without live promotion; pagination, readiness, calendar, and reconnect paths are covered. |
| Deterministic data and persistence | 0.20 | 7.0 | 1.400 | Universe/candle/idempotency/rebuild logic is implemented and locally tested; real PostgreSQL/Redis is blocked. |
| API, UI, and operability | 0.15 | 7.5 | 1.125 | Eight read endpoints, status-first accessible UI, valid UTF-8, localhost topology, and operating docs are coherent. Compose is blocked. |
| Verification quality | 0.10 | 7.5 | 0.750 | All runnable regressions, backend/static/frontend checks pass; seven infrastructure/live skips are explicit and have runnable gates. |

## Automatic-fail audit

| Rule | Result | Evidence |
|---|---|---|
| Credential leak | Clear | `.env` is ignored and absent; secret redaction and persistence-rejection checks pass. |
| Execution surface | Clear | OpenAPI contains exactly eight GET paths; UI/adapter scans contain no execution method or control. |
| Runtime fixture fallback | Clear | Runtime source has no fixture path/import and runtime image COPY allowlists exclude fixtures/tests. |
| False `LIVE` | Clear | Required quote `-120s` => `STALE`/503; `+120s` => `BLOCKED`/503. Future session/option/account/feed times block. Future LTP/depth events never become first-feed, quote, or candle state. |

## Adversarial regression evidence

- Required spot quote 120 seconds old: global `STALE`; market endpoint 503 `QUOTE_STALE`.
- Required spot quote 120 seconds future: global `BLOCKED`; market endpoint 503 `QUOTE_TIMESTAMP_FUTURE`.
- Market-session, option-chain, account, and feed timestamps 120 seconds future: each global `BLOCKED`.
- Option LTP 120 seconds future through the real worker callback path: `first_feed=False`, no option quote, no candle, global `STALE`.
- The same unit regression covers future market depth: both events are durably audited late and never promoted live.
- Silent feed: reconnects through bounded wait, cleans the old subscription, and establishes a second connection in the focused test.
- Live 5m acceptance: uses a dedicated 420-second deadline and requires a fresh complete five-minute window.
- UI files decode strictly as UTF-8; intended em dash, middle dot, and plus/minus glyphs are present with no replacement/double-encoding markers.

## Local checks

- Focused API/worker suites: **17 passed**.
- Full backend collection: **41 passed, 7 explicit skips**, one dependency deprecation warning.
- PostgreSQL/Redis integration is one of the seven explicit blocker skips.
- Frontend server-render test: passed.
- Next.js production build and TypeScript check: passed.
- Production frontend dependency audit: no known vulnerabilities.
- OpenAPI/read-only, UI safety, credential redaction/storage rejection, adapter surface, and runtime-fixture boundary tests: passed.
- `git diff --check`: passed apart from the pre-existing line-ending warning on `.gitignore`.

## Remaining critical/high findings

None.

## External blockers, not code passes

- Docker is not installed: PostgreSQL/Redis integration, Compose validation, runtime images/environments, non-root execution, and worker feed-tmpfs permissions remain unverified.
- No local `.env`/Groww TOTP credentials or subscription evidence is available: real authentication, broker contract compatibility, account reconciliation, and API-to-Groww comparison remain unverified.
- No credentialed exchange-open stack is available: live feed transitions and reconstructed 1m/5m market-hours evidence remain unverified.

The code-only GAN gate passes. Full MVP completion still requires every external gate above to pass with real services and Groww data.
