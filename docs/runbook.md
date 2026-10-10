# RelayCore local runbook

## Start and stop

- Start: `scripts/demo.ps1`; Compose binds the HTTP service to `127.0.0.1` only.
- To run a standalone worker locally, use `docker compose -f compose.yaml -f compose.workers.yaml up --build -d`. The override sets API `RELAYCORE_WORKERS=0` and runs `python -m app.worker` in its own container. The regular Demo Mode keeps API-managed workers so the dashboard can exercise kill/restart controls.
- Verify the split deployment end to end with `python -m scripts.compose_smoke`. It creates an isolated Compose project and database, queues the four-step sample workflow through the API, checks that a separate worker completes it, and removes the disposable stack and volume.
- On API startup, RelayCore applies pending numbered SQL migrations from `app/migrations/` while holding a PostgreSQL advisory lock. Back up production data before deploying schema changes; applied migration files must remain unchanged.
- In Demo Mode, create and publish definitions with `POST /api/workflow-definitions` and `POST /api/workflow-definitions/{id}/versions`, then queue the current version with `POST /api/workflow-definitions/{id}/runs`. Each queued run retains its published version and definition hash; local sandbox actions are available only in Demo Mode.
- Create an interval schedule with `POST /api/workspaces/{workspace_id}/schedules` and JSON `{"workflow_id":"<definition UUID>","name":"hourly check","interval_seconds":3600}` plus an `Idempotency-Key`. Intervals are 60–31,536,000 seconds; the first run is due after one interval. `GET` lists schedules, `PATCH` with `{"status":"paused"}` or `{"status":"active"}` pauses/resumes, and `DELETE` cancels. Resume begins a fresh interval. The coordinator retries the same due occurrence while admission is blocked by queue or rate limits. It schedules the current workflow version at dispatch time, but pauses if that version requires webhook event data. Cron/timezone rules are not supported.
- For a non-demo deployment, configure every `RELAYCORE_OIDC_*` value documented in `.env.example`, set `RELAYCORE_DEMO_MODE=0`, then visit `/auth/login`. The first signed-in user creates a workspace from the dashboard. API requests must send `X-Workspace-ID` when the user belongs to more than one workspace; the dashboard stores and sends the selected workspace automatically.
- To add an existing user, have them sign in once, retrieve their user ID from `GET /api/me`, then use the workspace member endpoint with an owner/admin session. Only owners can grant `OWNER` or `ADMIN`; only an owner can change another owner's membership. Email invitations are not implemented.
- Create a signed endpoint with `POST /api/workspaces/{workspace_id}/webhooks` and an `Idempotency-Key`; retain the returned secret securely because it is shown only on create/rotation. HMAC-SHA256 the exact body after the timestamp, a newline, the event ID, and another newline. Send the Unix timestamp, event ID, and `sha256=<hex>` signature in `X-RelayCore-Timestamp`, `X-RelayCore-Event-ID`, and `X-RelayCore-Signature`. `GET /api/events` and SSE expose secret-free metadata. Bind an exact JSON `type` to an immutable production workflow version for atomic, deduplicated trigger admission. The raw body and parsed JSON expire after `RELAYCORE_WEBHOOK_PAYLOAD_RETENTION_DAYS` (30 by default); the event ID, hash, and bounded type remain for deduplication/history. Nonterminal runs retain their source payload, and an event-dependent DLQ run cannot be replayed after its payload expires. The coordinator uses 500-row batches, retries full batches after one second, and otherwise checks every five minutes.
- In production, workflow steps are limited to bounded HTTP requests with an active, host-bound workspace credential and Slack messages with an active Slack App installation. Slack text can be static or a direct string reference to an earlier HTTP result; its conversation ID remains fixed. Configure the OIDC provider, Fernet key, allowlisted HTTP hosts, and optional GitHub/Slack Apps before enabling those paths. Demo Mode is for local examples and failure injection; do not use it as a shared production service.
- Status: open `/healthz` for database-backed liveness and `/api/status` for tenant-scoped operations data.
- Logs: `docker compose logs -f api`; request events include `X-Request-ID`, and persisted task history carries the same ID through worker claims and step transitions.
- Stop the API/database while preserving data: `docker compose down`.
- Remove the local demo database volume only when its stored state is disposable: `docker compose down -v`.

## Recovery controls

1. Create a demo workflow and wait until a worker owns its pause step.
2. Use that worker card's Kill button. The failure-injection event is appended before the HTTP transaction completes; it includes the worker ID and PID.
3. Wait for `task.lease_expired`, then `task.claimed` for a different worker, then `workflow.completed`.
4. To demonstrate the dead-letter path, create a demo hold, inspect `/api/dead-letters`, replay one row as admin, and observe the workflow complete. This replay action releases the deterministic demo hold; the original letter and audit events remain.
5. To inject another failure, restart the worker explicitly from its card.

## Configuration

| Variable | Default in local stack | Purpose |
|---|---:|---|
| `DATABASE_URL` | Compose PostgreSQL service | Durable data and queue |
| `RELAYCORE_API_KEYS` | `.env.example` demo/viewer keys | Secret-to-tenant/role mapping |
| `RELAYCORE_DEMO_MODE` | `1` | Enables deterministic failure-injection endpoints |
| `RELAYCORE_WORKERS` | `2` | Number of child processes supervised by the API; set to `0` when using a separate worker service |
| `RELAYCORE_QUEUE_LIMIT` | `1000` | Per-tenant active workflow admission bound |
| `RELAYCORE_SCHEDULE_LIMIT` | `1000` | Per-workspace active or paused schedule cap |
| `RELAYCORE_RATE_LIMIT_PER_MINUTE` | `600` | Per-tenant mutation bound |
| `RELAYCORE_WEBHOOK_PAYLOAD_RETENTION_DAYS` | `30` | Days to retain raw webhook bodies and parsed payloads; accepted range 1–3650 |
| `RELAYCORE_LEASE_SECONDS` | `4` | Task lease; must be at least 0.2 seconds. HTTP and Slack actions get one total DNS-plus-request timeout capped at half this lease. |
| `RELAYCORE_MAX_ATTEMPTS` | `3` | Bounded attempts per step |
| `RELAYCORE_OIDC_*` | unset | Required for production sign-in; values and HTTPS constraints are in `DEPLOYMENT.md` |
| `RELAYCORE_SECRET_ENCRYPTION_KEY` | unset | Required in production; Fernet key injected by a secret manager for encrypted webhook keys |

For separate deployment roles, set API `RELAYCORE_WORKERS=0` and run `python -m app.worker` in the worker service. Worker IDs default to hostname plus process ID; if hostnames are shared across replicas, configure a unique `RELAYCORE_WORKER_ID` on each. The database claim remains safe across competing workers through `SKIP LOCKED`. The regular Demo Mode Compose stack keeps API-managed workers so its failure-injection controls can terminate and restart workers.

## Backup and restore

- Set `DATABASE_URL` through the deployment secret manager, then run `scripts/backup.ps1 -Destination <path-outside-repository>`. It creates a PostgreSQL custom-format logical archive, refuses to overwrite, writes to a temporary file, and validates the archive before publishing it at the destination. It does not include cluster roles or ownership grants.
- Store backups in access-controlled encrypted storage. The script does not encrypt the archive. Keep retention and encryption policy in the storage layer.
- Database payload expiry clears the live row; older backups and retained WAL can still contain the event body. Set backup/WAL retention to match the data-handling policy.
- Restore only into a newly created empty database. Set `RELAYCORE_RESTORE_DATABASE_URL` to that database URL and run `scripts/restore.ps1 -BackupPath <archive> -TargetDatabaseUrl $env:RELAYCORE_RESTORE_DATABASE_URL`. PowerShell prompts before writing; `-WhatIf` previews the action. Restore uses one transaction, aborts on error, and never drops existing objects. Create DB roles and grants separately.
- After a restore, start RelayCore against the restored database and verify `/healthz`, migrations, and an authorized workflow read before directing traffic. A successful local restore is not evidence of managed-cloud backup retention or disaster recovery.

## Encryption key rotation

Run key rotation as a maintenance operation with all API and worker processes stopped. Back up the database first, then inject `DATABASE_URL`, the current `RELAYCORE_SECRET_ENCRYPTION_KEY`, and a new `RELAYCORE_SECRET_ENCRYPTION_NEW_KEY` from the secret manager and run `python -m scripts.rotate_secrets`. The command re-encrypts stored webhook keys, every integration credential version, and temporary GitHub PKCE verifiers in one transaction. If any value cannot be decrypted, the transaction rolls back. After success, set the active key to the new value on every service before restarting. Keep the old key in restricted escrow until pre-rotation backups expire; those backups still require it.

## Operational guarantees and limits

PostgreSQL is the only durable coordination dependency. Heartbeats and leases are database rows, so API/worker process restart does not discard an accepted task. The coordinator retries an expired lease with capped exponential backoff and stores terminal work in the DLQ. In the default local stack, an API restart starts configured worker children and a deliberately killed worker stays stopped until a human restarts it. In separate-role deployments, the orchestrator restarts worker containers according to its policy.

The built-in charge action writes a local effect record, not a real payment. A provider timeout after an external service performed work but before RelayCore recorded the response remains an ambiguous outcome; provider-side idempotency or an outbox/reconciliation flow is needed. This local failure test does not model every network partition or prove multi-region availability.
