# RelayCore

RelayCore runs bounded, multi-step workflows on independent worker processes. PostgreSQL stores workflow state, a durable work queue, leases, idempotency keys, event history, logical effects, retry state, and dead letters. The dashboard makes retries, worker failure, duplicate delivery, and queue pressure visible.

## Run the Demo Mode

Requirements: Docker Desktop running and Docker Compose v2. The script picks a free loopback port from 8000–8010 (or uses `RELAYCORE_PORT` when set).

```powershell
.\scripts\demo.ps1
```

The script creates `.env` from `.env.example`, builds the API image, starts PostgreSQL and two workers, checks health, and opens `http://127.0.0.1:8000`. The demo API key is `demo-key-change-me-32`; the viewer key is `viewer-key-change-me-32`. The app port binds to loopback. Change the keys before sharing or exposing the app.

Open the HTTP address printed by the demo script. Do not open `app/static/index.html` directly; the dashboard needs the API server to load live data.

In the dashboard:

1. Click **Run demo**. The workflow reserves inventory, pauses under a real worker lease, records a payment effect, and confirms shipment.
2. While a worker owns the pause step, click its **Kill** button. That button terminates the worker child process. The coordinator waits for its lease to expire, schedules a bounded retry, and another worker resumes the durable step.
3. Click **Send 10×**. One stable business event is delivered ten times; the UI shows ten deliveries, one workflow, and three logical database effects.
4. Click **Create DLQ case**. After three bounded failures, click **Replay** on its DLQ row. The replay action releases this deterministic demo hold, making the terminal transition observable.
5. Open `/docs` for the API, `/metrics` for Prometheus text, or use the dashboard event timeline.

The duplicate-event button uses a newly generated event key per click. Reuse an `event_key` through `POST /api/demo/duplicates` to repeat the same business event. Reusing that key with a different payload returns HTTP 409.

## Signed workspace webhooks

After production OIDC is configured, a workspace admin can create an endpoint with `POST /api/workspaces/{workspace_id}/webhooks` and an `Idempotency-Key`. Copy the returned signing secret immediately; the list API never returns it. Rotate with `POST /api/workspaces/{workspace_id}/webhooks/{endpoint_id}/rotate` and revoke with `DELETE` on that endpoint. Configure the sender to POST a JSON object to `/hooks/{endpoint_id}` with `X-RelayCore-Event-ID`, `X-RelayCore-Timestamp` (Unix seconds), and `X-RelayCore-Signature` (`sha256=<hex>`). The hex value is HMAC-SHA256 over `timestamp + newline + event ID + newline + exact request body`. Requests expire after five minutes, are limited to 256 KiB, and deduplicate by endpoint/event ID. `GET /api/events` returns paginated metadata without payloads. In Demo Mode, a workflow definition may trigger on an exact JSON `type`; production workflow creation/actions are still disabled, so production intake does not start runs yet.

Workspace owners and admins can also store bearer credentials with `POST /api/workspaces/{workspace_id}/credentials` (`provider`: `github`, `slack`, or `http`) and an `Idempotency-Key`; list, rotate, and revoke them through the matching `/credentials` routes. Values are Fernet-encrypted and never returned by management APIs. These are storage primitives only: provider OAuth and production actions are not yet implemented.

## Local development and checks

Python 3.13 and PostgreSQL 18 are the verified runtime. PostgreSQL must use UTF-8. Python 3.12 also fits the declared dependency ranges.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Start a PostgreSQL database with Docker, then run:

```powershell
.\scripts\test.ps1
```

Tests use a fresh tenant namespace and write no workflow data into the demo tenant. To use a dedicated existing test database, set `RELAYCORE_TEST_DATABASE_URL` before running the script. Do not point tests at production data.

To run the API without containers, set `DATABASE_URL` to a UTF-8 PostgreSQL database, set `RELAYCORE_DEMO_MODE=1`, provide `RELAYCORE_API_KEYS`, install the development requirements, and run:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The default database URL is intended only for build verification. Production startup requires HTTPS OIDC configuration and a managed PostgreSQL URL; static API keys are rejected outside Demo Mode. The browser signs in through `/auth/login`, creates its first workspace, and sends the selected workspace on each request. See [DEPLOYMENT.md](DEPLOYMENT.md) for the configuration contract. Live OIDC provider interoperability has not been verified because this repository has no provider credentials.

## Measure it

With `DATABASE_URL` set to an isolated UTF-8 PostgreSQL database:

```powershell
.\.venv\Scripts\python.exe -m benchmarks.run --workflows 100
```

The runner measures 1, 2, and 4 real worker processes and records throughput, P50/P95/P99 end-to-end latency, errors, peak active queue depth, and PostgreSQL `EXPLAIN ANALYZE` before/after adding an index to a temporary 20,000-row queue-shaped table. It writes a timestamped JSON report under `benchmarks/results/`. The workload is synthetic and local; those numbers are not external-provider, cloud, or production guarantees.

## Delivery and operating limits

- Delivery is at least once. A worker can repeat a step after lease expiry. A database uniqueness constraint makes the demonstrated logical database effect idempotent by tenant and step key.
- An external payment/email service must support its own idempotency key or be called through a transactional outbox with reconciliation. PostgreSQL cannot atomically commit an unrelated provider's side effect.
- Workflow creation, task claim, step effect/progress, cancellation, retries, replay, and their audit events use explicit PostgreSQL transaction boundaries.
- Demo actions are a fixed allow-list over data payloads. No workflow payload is evaluated as Python or shell code.
- Queue depth, workflow definitions, step count, per-step payloads, and tenant write rates have explicit limits. Saturation returns HTTP 429.
- Demo API keys map to an isolated sandbox tenant and role (`admin`, `operator`, or `viewer`). Production users are identified by the verified OIDC `(issuer, subject)` pair and receive workspace-scoped `OWNER`, `ADMIN`, `DEVELOPER`, or `VIEWER` permissions. Tenant IDs never come from the request body.
- The local stack is not a cloud deployment. No cloud account, deploy credentials, TLS endpoint, backups, or running Docker daemon were available as part of this build. Use managed PostgreSQL, TLS, secret rotation, backups, and an ingress policy before a public deployment.

See [ARCHITECTURE.md](ARCHITECTURE.md), [SECURITY.md](SECURITY.md), [TESTING.md](TESTING.md), [DEPLOYMENT.md](DEPLOYMENT.md), [BENCHMARKS.md](BENCHMARKS.md), [LIMITATIONS.md](LIMITATIONS.md), [docs/architecture.md](docs/architecture.md), [docs/adr/004-multitenancy.md](docs/adr/004-multitenancy.md), [docs/adr/005-secrets-management.md](docs/adr/005-secrets-management.md), [docs/adr/006-custom-webhook-ingress.md](docs/adr/006-custom-webhook-ingress.md), [docs/adr/007-webhook-trigger-matching.md](docs/adr/007-webhook-trigger-matching.md), [docs/adr/008-workspace-integration-credentials.md](docs/adr/008-workspace-integration-credentials.md), [docs/runbook.md](docs/runbook.md), and [PROJECT_STATUS.md](PROJECT_STATUS.md) for current guarantees and limits.

The measured failure/recovery run, PostgreSQL restart record, and dashboard capture are in [docs/evidence.md](docs/evidence.md). The local measured run uses an isolated database; it is not a public deployment.
