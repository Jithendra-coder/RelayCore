# RelayCore project status

## Current phase

Identity, workspace authorization, signed durable webhook intake, encrypted workspace credential storage, constrained production HTTP and Slack message actions, exact-match Demo/production triggers, GitHub pull-request events, Slack app-mention events, and durable interval schedules are implemented and locally tested. Production runs are versioned; HTTP body mapping is explicit and bounded. RelayCore remains a prototype: SDK/CLI, OpenTelemetry, staging, and production deployment are not complete.

## Phase gates

| Phase | Status | Evidence |
|---|---|---|
| P0 Repository audit and architecture decision | Complete | Preserve the PostgreSQL workflow/worker core; findings and invariants in `docs/architecture.md`. |
| P1–P5 Durable execution and recovery | Complete locally | Persistent queue, leases, retry/recovery, cancellation, idempotent database effects, DLQ/replay, and process/database restart evidence. |
| P6–P7 Tenant controls and observability | Complete locally | Demo roles, tenant-scoped authorization, request IDs, structured logs, SSE, Prometheus metrics, and dashboard. OpenTelemetry and operational alerts are still absent. |
| P8–P10 Failure tests and measurement | Complete locally | Worker failure, service/database restart, synthetic worker-count benchmark, and query-plan comparison. Evidence is local, not cloud or production proof. |
| Workflow versioning | Complete locally | Immutable versions, run/hash pinning, publication idempotency, and migration coverage. OIDC authors are recorded by user ID; sandbox authors retain credential fingerprints. |
| OIDC identity and workspaces | Complete locally; live provider unverified | Authlib callback, verified issuer/subject/email checks, expiring/revocable hashed sessions, workspace creation, role enforcement, origin checks, and cross-workspace tests. |
| Workspace-aware dashboard | Complete locally | Cookie-backed API requests, explicit workspace selection, sign-in/out, first-workspace creation, admin-only GitHub controls, and Slack connect/reconnect/disconnect controls. JavaScript syntax check passes. |
| Custom webhook ingress and event history | Complete locally | Encrypted endpoint secrets, HMAC over timestamp/event ID/raw body, five-minute replay window, 256 KiB cap, workspace rate limiting, idempotency, secret rotation/revocation, cursor-paged metadata, and API tests. |
| Workspace credential storage | Complete locally | GitHub, Slack, and generic bearer secrets are Fernet-encrypted, workspace-scoped, idempotently created/rotated, revocable, redacted from API/audit output, and covered by PostgreSQL tests. HTTP credentials bind to one operator-allowlisted host. |
| Production HTTP action | Complete locally; external endpoint unverified | Manual workflow runs send bounded JSON over verified HTTPS to a host-bound public destination; stable per-step idempotency key, bounded/redacted JSON result, no redirects, status-aware retries, and permanent-error DLQ path are tested with a no-network transport. |
| Production Slack message action | Complete locally; live Slack unverified | Versioned workflow steps use the workspace's encrypted Slack App token to post bounded static text to a conversation ID; fixed API URL, bounded response, no redirects, rate-limit delay, and permanent provider errors are covered by mocked action/workflow tests. Delivery is at least once and ambiguous failures can duplicate posts. |
| Separate API/worker process roles | Complete locally; staging unverified | `python -m app.worker` runs independently with hostname/PID worker IDs; optional `compose.workers.yaml` disables API child workers and checks the worker's recent PostgreSQL heartbeat. A PostgreSQL integration test confirms the external process claims and completes a run. |
| PostgreSQL logical backup tooling | Implemented; restore drill pending | `scripts/backup.ps1` writes and validates a custom archive; `scripts/restore.ps1` validates before a transactional, non-dropping restore. Local archive creation and restore preview passed; a full restore into a disposable target was not run. Managed retention is absent. |
| Existing-system audit | Complete | Source-based audit records reusable foundations, weak/missing areas, demo-only components, prioritized debt, and the decision to preserve the PostgreSQL core in `docs/audit/existing-system.md`. |
| Demo action isolation | Complete locally | Simulated `record`/`charge`/sleep/failure actions execute only through `app/sandbox.py`; production workers reject stale sandbox tasks before importing that module and dead-letter them without side effects. |
| Webhook workflow triggers | Complete locally for Demo Mode and constrained production actions | Exact endpoint ID + JSON `type`; event intake and matching run commit atomically. Duplicates do not enqueue twice. Production runs reference the source event, pin an immutable version, and support bounded JSON Pointer references in HTTP request bodies. |
| GitHub App linking and pull-request webhooks | Complete locally; live provider unverified | One-use workspace-admin state with PKCE; user access and App ownership checks; temporary token discarded; signed raw-body delivery, UUID dedupe, normalized PR events, installation status changes, dashboard controls, and durable workflow triggers covered by tests. GitHub API actions and live credentials remain absent. |
| Slack OAuth, Events API, and message action | Complete locally; live provider unverified | Workspace-admin OAuth state requests app-mention and message-post scopes; encrypted bot credential, active team uniqueness, raw-body HMAC plus five-minute timestamp check, URL verification, app-mention normalization, event dedupe, uninstall revocation, dashboard controls, durable workflow triggers, and bounded message posts are covered by tests. Live credentials remain absent. |
| Durable interval schedules | Complete locally; operations/UI limited | PostgreSQL stores schedule state; concurrent coordinator polls use `SKIP LOCKED`; queue pressure preserves a due occurrence; pause/resume/cancel are tenant-scoped; runs pin the current immutable version. Integration tests cover idempotency, lifecycle, concurrent dispatch, queue-full retention, version changes, event-dependent pauses, and schema upgrades. Cron/timezones and dashboard controls are absent. |
| Webhook payload retention | Complete locally; metadata expiry remains open | Coordinator clears raw bodies and parsed JSON after the configured retention period, protects nonterminal runs, retains dedupe/history metadata, and rejects DLQ replay after a referenced body expires. PostgreSQL integration coverage verifies expiry, duplicate delivery, and replay behavior. |
| Python dependency advisory scan | Implemented; GitHub check pending | Separate GitHub Actions workflow audits the resolved runtime and dev requirements on pushes, pull requests, manual runs, and weekly. The local `pip-audit` requirements scan found no known vulnerabilities on 2026-10-08. |
| Production secrets, deployment, SDK/CLI | Incomplete | See `LIMITATIONS.md`. |

## Latest validation

- PostgreSQL integration suite: **78 passed** on an isolated PostgreSQL 17.9 / UTF-8 database, 2026-10-08; **80% app source coverage**. Schedule coverage includes workspace RBAC, idempotency, pause/resume/cancel, two concurrent dispatchers, queue-full fairness across tenants, API coordinator dispatch, version pinning, and safe pause when event data becomes required. Webhook retention coverage verifies active-run protection, post-expiry duplicate detection, metadata history, and safe DLQ rejection. The current GitHub Actions workflow also runs the suite on PostgreSQL 18.
- Ruff, pre-commit, compilation, and `git diff --check` pass. Coverage is a report, not a configured threshold.
- The logical backup helper created a valid PostgreSQL custom archive; `restore.ps1 -WhatIf` validated its catalog without changing a database. A successful restore drill is still pending.
- `python -m compileall -q app tests benchmarks scripts` passes. Runtime limits fail fast when worker count, attempt range, queue/schedule/rate limits, payload retention, or lease duration are invalid.
- Local live Demo Mode evidence: 10 duplicate deliveries -> 1 workflow -> 3 effects; a killed worker was replaced after lease expiry; DLQ replay completed. Queue depth ended at 0. The recorded P95 includes recovery delay.
- Synthetic 100-workflow P95: 3,096.99 ms (1 worker), 2,364.86 ms (2), 2,289.27 ms (4); 0/100 failures each. A local 20,000-row query measured 15.411 ms before and 0.126 ms after a partial index in one run. See `BENCHMARKS.md` and `docs/evidence.md`.
- GitHub Actions run [37770598389](https://github.com/Jithendra-coder/RelayCore/actions/runs/37770598389) passed the retention commit on PostgreSQL 18, including Ruff, pre-commit, the full test suite, both Compose configurations, and the image build. Docker, a live OIDC provider, external integrations, a dependency-audit Actions run, and cloud deployment were not run locally.

## Known limits and technical debt

- A configured OIDC provider and credentials are required for actual browser sign-in; only local claim/configuration behavior and cookie-backed sessions were tested here.
- Production API startup requires OIDC and a Fernet encryption key; static keys are Demo Mode only. Production workflow submission accepts constrained HTTP and connected-workspace Slack message steps, with manual starts, exact event triggers, or durable interval schedules.
- GitHub App linking and pull-request webhook intake, Slack OAuth/app-mention intake, and Slack message posting are locally tested with mocked provider responses; no live GitHub, Slack, or OIDC credentials are configured. GitHub API actions, provider-side GitHub uninstall, HTTP response mapping, and dynamic URL/header/message mapping remain open. External actions remain at least once; HTTP providers must honor the stable idempotency key, while Slack can duplicate posts after ambiguous responses. An in-flight call can finish after cancellation, with its response recorded. The worker rejects sandbox actions in production even if old demo rows remain queued.
- Workflow conditions/branches, approvals, cron/timezone schedules, and a transactional external-action outbox remain open. Slack `Retry-After` delays are honored; richer provider-specific limit handling remains open.
- Raw webhook bodies and parsed JSON expire on a configurable schedule; event metadata and dedupe keys remain indefinitely, and endpoint-specific schemas are absent. The encryption master key has no automated rotation; public deployment also needs edge IP/network limits.
- PostgreSQL is the source of truth and queue. The per-tenant event-order lock can bottleneck high-volume writes. Independent broker scaling/replay has not been measured or justified.
- The default local stack can supervise worker children under the API. For separate roles, set `RELAYCORE_WORKERS=0` on the API and run standalone worker processes with unique IDs before scaling API replicas.
- OpenTelemetry traces, pool metrics, Grafana dashboards, alert rules, mypy/Pyright, a coverage threshold, container/OS image scanning, staging, infrastructure-as-code, rollback, and production deployment remain missing.
- The public repository still needs the remaining integration/deployment milestones before this project can be called complete.

## Latest change

Added configurable webhook payload expiry with run-safe cleanup, durable duplicate detection after expiry, and explicit 409 handling for DLQ replay after a required source body is purged. Schedule support remains in the prior milestone. Live provider credentials and cloud release remain unverified.

## Next milestone

Complete the existing PostgreSQL backup/restore drill, then add dependency vulnerability scanning. Live GitHub, Slack, and OIDC credentials are prerequisites for external interoperability checks.
