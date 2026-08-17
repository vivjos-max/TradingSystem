# Phase 0 preservation

Preserved on 2026-08-17 before reconciliation work.

- Recovery branch: `codex/phase0-current-recovery-20260817`
- Source commit: `1d06e4006656e566fdc4aa9de368e8e33ac8a3f7`
- Unchanged main reference: `1d06e4006656e566fdc4aa9de368e8e33ac8a3f7`
- Unchanged remote implementation reference: `origin/codex/live-read-only-foundation` at `e8b9100aee6e9e56febfcd3d7ef1b844f275d111`
- Scope: every modified or untracked, non-ignored file reported by Git, including the backend, frontend, tests, operations documents, evaluation artifacts, plans, and scripts.
- Excluded by existing ignore rules: local credentials, virtual environments, dependency directories, build output, caches, coverage output, generated Next.js declarations, and live-acceptance artifacts.

No database data, Redis state, broker state, remote branch, or remote repository was changed.

## Rollback

Switch back to this branch to recover the preserved implementation. `main` and the remote implementation branch remain separate comparison points; do not rewrite either history.
