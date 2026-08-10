# Private live NIFTY dashboard

This stack runs a localhost-only, read-only Groww dashboard. The browser and API can read normalized market/account state; only the worker receives Groww TOTP credentials. There are no order mutation routes, broker execution methods, or fixture fallbacks.

## Start locally

1. Purchase and activate the Groww API subscription for the account you intend to observe.
2. Create Groww TOTP credentials for that API subscription.
3. Copy the checked-in template without committing the result:

   ```powershell
   Copy-Item .env.example .env
   ```

4. Fill only local secrets in `.env`: `GROWW_TOTP_TOKEN`, `GROWW_TOTP_SECRET`, and optional local PostgreSQL/Redis passwords. Never add an access token; access tokens are recreated in worker memory on authentication and reconnect.
5. Start the five-service stack:

   ```powershell
   docker compose up -d --build
   ```

6. Check [http://127.0.0.1:8000/health/ready](http://127.0.0.1:8000/health/ready). A `503` is expected until dependencies, reconciliation, and the first live feed update all succeed.
7. Open [http://127.0.0.1:3000](http://127.0.0.1:3000).
8. Stop the stack when finished:

   ```powershell
   docker compose down
   ```

PostgreSQL and Redis are available only to the private Compose network. The published dashboard ports bind to `127.0.0.1`, not the LAN.

## Developer setup and checks

```powershell
.\scripts\bootstrap.ps1
.\scripts\test.ps1
.\scripts\test.ps1 -Integration
```

Bootstrap uses Python 3.12, refuses Python below 3.11, refuses Node below 20.9, installs the hash-locked backend requirements, and installs the frozen pnpm lockfile.

## Stable error codes

- `LIVE_STATE_UNAVAILABLE`: the API is alive but readiness or a required normalized snapshot is absent. Inspect `docker compose ps` and `docker compose logs worker`; wait for reconciliation and a first live update.
- `SETTINGS_VALIDATION_FAILED`: an environment field has the wrong type or shape. Compare names and non-secret formats with `.env.example`.
- `GROWW_AUTH_FAILED`: verify the API subscription, TOTP token, TOTP secret, and system clock. The worker remains blocked and retries; it does not fall back to fixtures.
- `GROWW_PERMISSION_DENIED`: enable the required read permissions in Groww. No additional trading permissions are needed by this stack.
- `GROWW_RATE_LIMITED`: allow the bounded worker retry to recover; avoid repeatedly restarting it.
- `GROWW_TIMEOUT`: check internet connectivity and Groww status; the last state remains unavailable/stale rather than fabricated.
- `GROWW_CONTRACT_MISMATCH`: the live response did not match the allowlisted normalized contract. Stop relying on the affected panel and inspect the worker correlation ID without logging raw broker payloads.

For infrastructure failures, `docker compose logs postgres redis api worker` shows service-local diagnostics. Do not paste `.env`, TOTP values, or access tokens into logs or support tickets.
