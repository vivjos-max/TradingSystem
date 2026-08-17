# Code-Only Evaluation Rubric

Pass threshold: **7.0/10**. Any credential leak, execution surface, runtime fixture fallback, or false `LIVE` state is an automatic failure regardless of score.

| Criterion | Weight | A passing implementation |
|---|---:|---|
| Read-only security boundary | 0.30 | Secrets are redacted/isolated; SDK mutations and private escape hatches are absent; OpenAPI/UI are GET-only. |
| Live correctness and failure honesty | 0.25 | Real data is required; reconciliation/pagination are exhaustive; freshness and readiness fail closed. |
| Deterministic data and persistence | 0.20 | Universe selection, event-time candles, idempotency, and Redis rebuild behavior are tested. |
| API, UI, and operability | 0.15 | Eight endpoints, accessible status-first dashboard, localhost/container boundaries, and usable docs exist. |
| Verification quality | 0.10 | Available tests/builds pass; unavailable Docker/live gates are reported as blockers with runnable checks. |

## Scoring

- 1–3: core safety/correctness missing.
- 4–6: partial vertical slice with material gaps.
- 7–8: approved MVP behavior implemented with minor non-blocking issues.
- 9–10: all automated and live acceptance evidence complete.
