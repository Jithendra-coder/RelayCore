# RelayCore local runbook

## Start and stop

- Start: `scripts/demo.ps1`; Compose binds the HTTP service to `127.0.0.1` only.
- On API startup, RelayCore applies pending numbered SQL migrations from `app/migrations/` while holding a PostgreSQL advisory lock. Back up production data before deploying schema changes; applied migration files must remain unchanged.
- In Demo Mode, create and publish definitions with `POST /api/workflow-definitions` and `POST /api/workflow-definitions/{id}/versions`, then queue the current version with `POST /api/workflow-definitions/{id}/runs`. Each queued run retains its published version and definition hash; local sandbox actions are available only in Demo Mode.
- For a non-demo deployment, configure every `RELAYCORE_OIDC_*` value documented in `.env.example`, set `RELAYCORE_DEMO_MODE=0`, then visit `/auth/login`. The first signed-in user creates a workspace from the dashboard. API requests must send `X-Workspace-ID` when the user belongs to more than one workspace; the dashboard stores and sends the selected workspace automatically.
- To add an existing user, have them sign in once, retrieve their user ID from `GET /api/me`, then use the workspace member endpoint with an owner/admin session. Only owners can grant `OWNER` or `ADMIN`; only an owner can change another owner's membership. Email invitations are not implemented.
- Create a signed endpoint with `POST /api/workspaces/{workspace_id}/webhooks` and an `Idempotency-Key`; retain the returned secret securely because it is shown only on create/rotation. HMAC-SHA256 the exact body after the timestamp, a newline, the event ID, and another newline. Send the Unix timestamp, event ID, and `sha256=<hex>` signature in `X-RelayCore-Timestamp`, `X-RelayCore-Event-ID`, and `X-RelayCore-Signature`. `GET /api/events` and SSE expose secret-free metadata. Bind an exact JSON `type` to an immutable production workflow version for atomic, deduplicated trigger admission.
- In production, workflow steps are limited to bounded HTTP requests with an active, host-bound workspace credential and fixed Slack messages with an active Slack App installation. Configure the OIDC provider, Fernet key, allowlisted HTTP hosts, and optional GitHub/Slack Apps before enabling those paths. Demo Mode is for local examples and failure injection; do not use it as a shared production service.
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
| `RELAYCORE_WORKERS` | `2` | Number of child processes supervised by the API |
| `RELAYCORE_QUEUE_LIMIT` | `1000` | Per-tenant active workflow admission bound |
| `RELAYCORE_RATE_LIMIT_PER_MINUTE` | `600` | Per-tenant mutation bound |
| `RELAYCORE_LEASE_SECONDS` | `4` | Lease renewed while the step is active |
| `RELAYCORE_MAX_ATTEMPTS` | `3` | Bounded attempts per step |
| `RELAYCORE_OIDC_*` | unset | Required for production sign-in; values and HTTPS constraints are in `DEPLOYMENT.md` |
| `RELAYCORE_SECRET_ENCRYPTION_KEY` | unset | Required in production; Fernet key injected by a secret manager for encrypted webhook keys |

Do not run multiple API supervisor instances with the same worker IDs. For horizontal API scaling, move worker lifecycle supervision into a separately deployed worker service and use unique worker IDs. The database claim remains safe across competing workers through `SKIP LOCKED`.

## Operational guarantees and limits

PostgreSQL is the only durable coordination dependency. Heartbeats and leases are database rows, so API/worker process restart does not discard an accepted task. The coordinator retries an expired lease with capped exponential backoff and stores terminal work in the DLQ. An API restart starts configured worker children; a deliberately killed worker stays stopped until a human restarts it.

The built-in charge action writes a local effect record, not a real payment. A provider timeout after an external service performed work but before RelayCore recorded the response remains an ambiguous outcome; provider-side idempotency or an outbox/reconciliation flow is needed. This local failure test does not model every network partition or prove multi-region availability.
