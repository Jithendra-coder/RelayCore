# RelayCore project status

## Current phase

Identity, workspace authorization, signed durable webhook intake, encrypted workspace credential storage, constrained production HTTP actions, exact-match Demo/production triggers, and GitHub App pull-request events are implemented and locally tested. Production runs are versioned and HTTP-only; webhook body data mapping is explicit and bounded. RelayCore remains a prototype: Slack and provider-native actions, scheduling, and deployment are not complete.

## Phase gates

| Phase | Status | Evidence |
|---|---|---|
| P0 Repository audit and architecture decision | Complete | Preserve the PostgreSQL workflow/worker core; findings and invariants in `docs/architecture.md`. |
| P1–P5 Durable execution and recovery | Complete locally | Persistent queue, leases, retry/recovery, cancellation, idempotent database effects, DLQ/replay, and process/database restart evidence. |
| P6–P7 Tenant controls and observability | Complete locally | Demo roles, tenant-scoped authorization, request IDs, structured logs, SSE, Prometheus metrics, and dashboard. OpenTelemetry and operational alerts are still absent. |
| P8–P10 Failure tests and measurement | Complete locally | Worker failure, service/database restart, synthetic worker-count benchmark, and query-plan comparison. Evidence is local, not cloud or production proof. |
| Workflow versioning | Complete locally | Immutable versions, run/hash pinning, publication idempotency, and migration coverage. OIDC authors are recorded by user ID; sandbox authors retain credential fingerprints. |
| OIDC identity and workspaces | Complete locally; live provider unverified | Authlib callback, verified issuer/subject/email checks, expiring/revocable hashed sessions, workspace creation, role enforcement, origin checks, and cross-workspace tests. |
| Workspace-aware dashboard | Complete locally | Cookie-backed API requests, explicit workspace selection, sign-in/out, first-workspace creation, and admin-only GitHub connect/disconnect controls. JavaScript syntax check passes. |
| Custom webhook ingress and event history | Complete locally | Encrypted endpoint secrets, HMAC over timestamp/event ID/raw body, five-minute replay window, 256 KiB cap, workspace rate limiting, idempotency, secret rotation/revocation, cursor-paged metadata, and API tests. |
| Workspace credential storage | Complete locally | GitHub, Slack, and generic bearer secrets are Fernet-encrypted, workspace-scoped, idempotently created/rotated, revocable, redacted from API/audit output, and covered by PostgreSQL tests. HTTP credentials bind to one operator-allowlisted host. |
| Production HTTP action | Complete locally; external endpoint unverified | Manual workflow runs send bounded JSON over verified HTTPS to a host-bound public destination; stable per-step idempotency key, bounded/redacted JSON result, no redirects, status-aware retries, and permanent-error DLQ path are tested with a no-network transport. |
| Existing-system audit | Complete | Source-based audit records reusable foundations, weak/missing areas, demo-only components, prioritized debt, and the decision to preserve the PostgreSQL core in `docs/audit/existing-system.md`. |
| Demo action isolation | Complete locally | Simulated `record`/`charge`/sleep/failure actions execute only through `app/sandbox.py`; production workers reject stale sandbox tasks before importing that module and dead-letter them without side effects. |
| Webhook workflow triggers | Complete locally for Demo Mode and constrained production HTTP | Exact endpoint ID + JSON `type`; event intake and matching run commit atomically. Duplicates do not enqueue twice. Production runs are HTTP-only, reference the source event, pin an immutable version, and support bounded JSON Pointer references in request bodies. |
| GitHub App linking and pull-request webhooks | Complete locally; live provider unverified | One-use workspace-admin state with PKCE; user access and App ownership checks; temporary token discarded; signed raw-body delivery, UUID dedupe, normalized PR events, installation status changes, dashboard controls, and durable workflow triggers covered by tests. GitHub API actions and live credentials remain absent. |
| Slack integration | Incomplete | Credential storage exists, but Slack OAuth, event intake, native actions, and live interoperability are not implemented. |
| Production secrets, scheduling, deployment, SDK/CLI | Incomplete | See `LIMITATIONS.md`. |

## Latest validation

- PostgreSQL integration suite: **53 passed** on PostgreSQL 18 / UTF-8, 2026-10-05; **79% app source coverage**. Includes OIDC/RBAC, secret handling, workspace and credential isolation/rotation/host binding, SSRF allowlist/DNS pinning, bounded HTTP responses and rendered request bodies, status-aware retries, cancellation during an HTTP call, fresh/upgrade migrations, concurrency, signed production webhook triggers/version pinning, worker action-mode isolation, SSE cursor reading, and GitHub OAuth ownership checks, installation paging, signed PR delivery, deduplication, and disconnect/reconnect behavior.
- Ruff and pre-commit pass. Coverage is a report, not a configured threshold.
- `python -m compileall -q app tests benchmarks scripts` passes; the dashboard script passes `node --check`; `git diff --check` passes.
- Local live Demo Mode evidence: 10 duplicate deliveries -> 1 workflow -> 3 effects; a killed worker was replaced after lease expiry; DLQ replay completed. Queue depth ended at 0. The recorded P95 includes recovery delay.
- Synthetic 100-workflow P95: 3,096.99 ms (1 worker), 2,364.86 ms (2), 2,289.27 ms (4); 0/100 failures each. A local 20,000-row query measured 15.411 ms before and 0.126 ms after a partial index in one run. See `BENCHMARKS.md` and `docs/evidence.md`.
- The current verification did not run Docker, a live OIDC provider, external integrations, or a cloud deployment.

## Known limits and technical debt

- A configured OIDC provider and credentials are required for actual browser sign-in; only local claim/configuration behavior and cookie-backed sessions were tested here.
- Production API startup requires OIDC and a Fernet encryption key; static keys are Demo Mode only. Production workflow submission accepts only allowlisted HTTP steps and manual starts.
- GitHub App linking and pull-request webhook intake are locally tested with mocked GitHub API responses; no live GitHub App credentials are configured. Slack OAuth/webhooks, provider-native actions, provider-side GitHub uninstall, HTTP response mapping, and dynamic URL/header mapping remain open. External HTTP delivery remains at least once; the remote provider must honor the stable idempotency key. An in-flight call can finish after cancellation, with its response recorded. The worker rejects sandbox actions in production even if old demo rows remain queued.
- Durable schedules, workflow conditions/branches, approvals, provider-specific rate-limit handling, and a transactional external-action outbox remain open.
- Incoming webhook payloads have no retention/cleanup or endpoint-specific schema. The encryption master key has no automated rotation; public deployment also needs edge IP/network limits.
- PostgreSQL is the source of truth and queue. The per-tenant event-order lock can bottleneck high-volume writes. Independent broker scaling/replay has not been measured or justified.
- API and worker supervisor currently run together. Do not horizontally scale API supervisors with duplicate worker IDs; split workers and assign unique IDs first.
- OpenTelemetry traces, pool metrics, Grafana dashboards, alert rules, mypy/Pyright, a coverage threshold, dependency scanning, staging, infrastructure-as-code, rollback, and production deployment remain missing.
- The public repository still needs the remaining integration/deployment milestones before this project can be called complete.

## Latest change

Added GitHub App workspace linking, signed pull-request intake, and dashboard controls. Supported events enter the existing deduplicated, version-pinned workflow trigger transaction; installation status and disconnect stop further triggers. Live GitHub, live OIDC, and cloud release remain unverified.

## Next milestone

Verify the GitHub flow using configured GitHub App credentials and a live delivery. Then continue with Slack, schedules, SDK/CLI, telemetry, and deployment.
