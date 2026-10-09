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

`/metrics` is workspace-authenticated and includes `relaycore_queue_oldest_ready_seconds` and `relaycore_expired_leases` for queue-lag and lease alerts, alongside queue depth and dead-letter gauges. Owners and admins can issue read-only workspace metrics tokens for Prometheus. See [docs/operations.md](docs/operations.md) for token handling and the Prometheus scrape and alert examples.

Optional distributed traces use OTLP/HTTP. Set `OTEL_EXPORTER_OTLP_ENDPOINT` to a reachable collector base URL (for example, `http://otel-collector:4318`) to export API request and worker step spans. Tracing is disabled when no OTLP endpoint is set. RelayCore propagates W3C `traceparent` through durable tasks, stores no baggage, and excludes workflow payloads and exception messages from spans.

The duplicate-event button uses a newly generated event key per click. Reuse an `event_key` through `POST /api/demo/duplicates` to repeat the same business event. Reusing that key with a different payload returns HTTP 409.

## Signed workspace webhooks

After production OIDC is configured, a workspace admin can create an endpoint with `POST /api/workspaces/{workspace_id}/webhooks` and an `Idempotency-Key`. Copy the returned signing secret immediately; the list API never returns it. Rotate with `POST /api/workspaces/{workspace_id}/webhooks/{endpoint_id}/rotate` and revoke with `DELETE` on that endpoint. Configure the sender to POST a JSON object to `/hooks/{endpoint_id}` with `X-RelayCore-Event-ID`, `X-RelayCore-Timestamp` (Unix seconds), and `X-RelayCore-Signature` (`sha256=<hex>`). The hex value is HMAC-SHA256 over `timestamp + newline + event ID + newline + exact request body`. Requests expire after five minutes, are limited to 256 KiB, and deduplicate by endpoint/event ID. `GET /api/events` returns paginated metadata without payloads. Raw bodies and parsed JSON expire after 30 days by default; event hashes and dedupe metadata remain. Demo Mode supports exact JSON `type` triggers for sandbox actions. In production, an exact `type` match starts one immutable HTTP/Slack workflow version in the event transaction; a duplicate delivery does not create another run.

Workspace owners and admins can store bearer credentials with `POST /api/workspaces/{workspace_id}/credentials` (`provider`: `github`, `slack`, or `http`) and an `Idempotency-Key`; list, rotate, and revoke them through the matching `/credentials` routes. Values are Fernet-encrypted and never returned by management APIs. Generic HTTP credentials also require `allowed_host`, which must exactly match a configured `RELAYCORE_HTTP_ALLOWED_HOSTS` entry.

Production workflows currently accept versioned HTTP steps and Slack message steps. Start them manually, or attach an exact custom-webhook `type` trigger. HTTP bodies can reference event fields with a JSON Pointer object:

```json
{"name":"Notify build service","action":"http","payload":{"method":"POST","url":"https://hooks.example.com/v1/events","credential_id":"<credential-id>","body":{"repository":{"$event":"/repository/full_name"}}}}
```

Put the step in a `POST /api/workflow-definitions` request and include `"trigger":{"endpoint_id":"<endpoint-id>","event_type":"push"}` to run it from signed webhooks. A body using `$event` references requires a webhook trigger and cannot be started manually. `POST /api/workflows` is reserved for Demo Mode.

## Durable interval schedules

Create and manage schedules in the dashboard’s **Interval schedules** panel or through `POST /api/workspaces/{workspace_id}/schedules`. Workspace operators and admins can create, pause, resume, and cancel schedules; viewers can list them. Intervals range from 60 seconds to one year. The first occurrence is due after the full interval. `GET` lists schedules, `PATCH` pauses or resumes with `{"status":"paused"}` / `{"status":"active"}`, and `DELETE` cancels. Include an `Idempotency-Key` when creating. Active/paused schedules are limited per workspace and mutations use the configured write rate limit. Resume starts a fresh interval; queue or rate-limit pressure leaves the due occurrence pending. Each accepted run pins the version current at dispatch. If a later version needs webhook event data, the schedule pauses with a reason. The coordinator polls around once per second; cron and timezone calendars are not supported. See [the runbook](docs/runbook.md) for a request example and operational behavior.

## Python SDK and CLI

Production workspace owners and admins can create expiring, workspace-bound API tokens from the dashboard's **API access** panel. New tokens default to operator access; viewers are read-only, and admin access must be selected explicitly. The effective permission never exceeds the owner's current workspace role. Tokens are shown once, stored only as hashes, and can be revoked from the same panel. Install the typed, standard-library client from this checkout with `python -m pip install -e .`, then use `relaycore workflow list`, `relaycore workflow run <id>`, and `relaycore runs list`. Set `RELAYCORE_URL`, `RELAYCORE_WORKSPACE_ID`, and `RELAYCORE_API_TOKEN` in the process environment. The complete command and Python usage guide is in [docs/sdk-cli.md](docs/sdk-cli.md).

## GitHub App events

RelayCore can link one GitHub App installation to each workspace and turn signed pull-request deliveries into ordinary durable webhook events. Run production OIDC and configure all `RELAYCORE_GITHUB_*` values in [`.env.example`](.env.example). In the GitHub App settings, set the setup and callback URLs to those environment values, set the webhook URL to `https://<your-host>/integrations/github/webhook`, choose the same webhook secret, grant **Pull requests: read-only**, and subscribe to `pull_request` and `installation` events. Do not enable GitHub's automatic OAuth-on-install redirect; RelayCore uses the separate setup URL to bind the installation to the signed-in workspace admin.

An admin starts the flow with `POST /api/workspaces/{workspace_id}/github/install`, opens its returned `install_url`, then returns to RelayCore to complete OAuth. RelayCore verifies that the GitHub user can access the selected installation and that the installation belongs to the configured App; the temporary user token is not stored. `GET /api/workspaces/{workspace_id}/github` returns connection status and the internal webhook endpoint ID to use in a workflow trigger. `DELETE` on that path disconnects the workspace integration. A trigger event type is `github.pull_request.opened`, `.reopened`, `.synchronize`, or `.closed`; the normalized payload contains a bounded set of PR and repository fields. GitHub's `X-Hub-Signature-256` is checked against the exact raw body, and `X-GitHub-Delivery` deduplicates deliveries. See GitHub's [setup URL security guidance](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-setup-url), [user installation access](https://docs.github.com/en/rest/apps/installations#list-app-installations-accessible-to-the-user-access-token), and [webhook signature validation](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries).

The GitHub flow is covered with mocked provider responses and PostgreSQL integration tests, not live GitHub credentials. The workspace dashboard exposes admin-only connect/disconnect controls. GitHub API actions and provider-side uninstall remain incomplete.

## Slack App events and messages

Configure the optional `RELAYCORE_SLACK_*` values in [`.env.example`](.env.example). In Slack App settings, register the HTTPS callback and Events API URL from those values, subscribe to `app_mention` and `app_uninstalled`, and grant `app_mentions:read` and `chat:write`. An owner/admin connects the Slack workspace from the RelayCore dashboard. Reconnect there after changing the app's scopes so the stored token receives the updated permissions. RelayCore binds OAuth state to that administrator and workspace, encrypts the returned bot token, verifies Slack's five-minute timestamped HMAC over the raw request, and durably stores normalized `slack.app_mention` events without retaining Slack's legacy callback token. Use the internal endpoint ID from `GET /api/workspaces/{workspace_id}/slack` in an exact workflow trigger. The bot must be invited to the target conversation. Disconnecting in RelayCore revokes the stored token locally; it does not uninstall the Slack app.

Production workflow steps can post a fixed message to a Slack conversation ID with `{"name":"Announce","action":"slack_message","payload":{"channel":"C0123456789","text":"Build completed."}}`. RelayCore uses the connected workspace app token; step payloads cannot supply credentials or arbitrary URLs. Slack DNS lookups are bounded, the checked public address is pinned for HTTPS, and one DNS-through-response deadline is capped at half the worker lease. Slack message delivery is at least once: a duplicate is possible if Slack accepts a post but the worker loses the response before it saves completion. Slack API behavior is tested with mocked responses and has not been verified with live credentials. See Slack's [OAuth v2 guide](https://api.slack.com/authentication/oauth-v2), [signature validation guidance](https://api.slack.com/docs/verifying-requests-from-slack), [URL verification](https://api.slack.com/events/url_verification), and [`app_mention` event](https://api.slack.com/events/app_mention).

HTTP steps require HTTPS on port 443, a host-bound credential, public DNS answers, no redirects, and a 64 KiB response limit. One deadline of at most one second covers DNS, connection, and response handling; the worker further caps it at half the lease. The connection pins its checked public address and verifies TLS against the hostname. Timed-out DNS does not open a provider connection; bounded resolver capacity prevents stuck lookups from accumulating without limit. Credential-like URL query/header fields are rejected; JSON response keys and strings echoing the bearer token are redacted. RelayCore sends a stable `Idempotency-Key` (`run-id:step-index`) and retries 408, 429, and 5xx; the remote service must honor that key because delivery is still at least once. Triggered workflows can map event fields into the JSON body with `{"$event":"/repository/full_name"}` JSON Pointer objects. Mapping is limited to the body; URLs, headers, and later-step responses are not dynamic. Missing references or rendered bodies over 4 KiB go to the DLQ. Runs retain the event ID and safe metadata, while run-history APIs omit the payload. See [docs/audit/existing-system.md](docs/audit/existing-system.md) for the source-based audit and rebuild decision.

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

The default database URL is intended only for build verification. Production startup requires HTTPS OIDC configuration and a managed PostgreSQL URL with `sslmode=verify-full`; the API and worker reject implicit or weaker database SSL modes. Static API keys are rejected outside Demo Mode. The browser signs in through `/auth/login`, creates its first workspace, and sends the selected workspace on each request. See [DEPLOYMENT.md](DEPLOYMENT.md) for the configuration contract. Live OIDC and managed-database TLS interoperability have not been verified because this repository has no provider credentials or staging database.

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
- The local stack is not a cloud deployment. PostgreSQL logical backup/restore helpers and an offline database encryption-key rotation command are in `scripts/`, but managed backup retention and disaster recovery have not been verified. No cloud account, deploy credentials, or TLS endpoint are configured. Use managed PostgreSQL, TLS, secret rotation, tested backups, and an ingress policy before public deployment. See [DEPLOYMENT.md](DEPLOYMENT.md) for the maintenance-window key-rotation procedure.

See [ARCHITECTURE.md](ARCHITECTURE.md), [SECURITY.md](SECURITY.md), [TESTING.md](TESTING.md), [DEPLOYMENT.md](DEPLOYMENT.md), [BENCHMARKS.md](BENCHMARKS.md), [LIMITATIONS.md](LIMITATIONS.md), [docs/architecture.md](docs/architecture.md), [docs/adr/004-multitenancy.md](docs/adr/004-multitenancy.md), [docs/adr/005-secrets-management.md](docs/adr/005-secrets-management.md), [docs/adr/006-custom-webhook-ingress.md](docs/adr/006-custom-webhook-ingress.md), [docs/adr/007-webhook-trigger-matching.md](docs/adr/007-webhook-trigger-matching.md), [docs/adr/008-workspace-integration-credentials.md](docs/adr/008-workspace-integration-credentials.md), [docs/adr/009-constrained-http-actions.md](docs/adr/009-constrained-http-actions.md), [docs/adr/010-production-webhook-triggers.md](docs/adr/010-production-webhook-triggers.md), [docs/adr/011-workspace-api-tokens.md](docs/adr/011-workspace-api-tokens.md), [docs/runbook.md](docs/runbook.md), and [PROJECT_STATUS.md](PROJECT_STATUS.md) for current guarantees and limits.

The measured failure/recovery run, PostgreSQL restart record, and dashboard capture are in [docs/evidence.md](docs/evidence.md). The local measured run uses an isolated database; it is not a public deployment.
