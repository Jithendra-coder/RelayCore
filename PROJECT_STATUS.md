# RelayCore project status

## Current phase

Identity, workspace authorization, signed durable webhook intake, encrypted workspace credential storage, and exact-match sandbox workflow triggers are implemented and locally tested. The next product phase is a safe production action adapter. RelayCore remains a prototype: production workflow actions, durable scheduling, and deployment are not complete.

## Phase gates

| Phase | Status | Evidence |
|---|---|---|
| P0 Repository audit and architecture decision | Complete | Preserve the PostgreSQL workflow/worker core; findings and invariants in `docs/architecture.md`. |
| P1–P5 Durable execution and recovery | Complete locally | Persistent queue, leases, retry/recovery, cancellation, idempotent database effects, DLQ/replay, and process/database restart evidence. |
| P6–P7 Tenant controls and observability | Complete locally | Demo roles, tenant-scoped authorization, request IDs, structured logs, SSE, Prometheus metrics, and dashboard. OpenTelemetry and operational alerts are still absent. |
| P8–P10 Failure tests and measurement | Complete locally | Worker failure, service/database restart, synthetic worker-count benchmark, and query-plan comparison. Evidence is local, not cloud or production proof. |
| Workflow versioning | Complete locally | Immutable versions, run/hash pinning, publication idempotency, and migration coverage. OIDC authors are recorded by user ID; sandbox authors retain credential fingerprints. |
| OIDC identity and workspaces | Complete locally; live provider unverified | Authlib callback, verified issuer/subject/email checks, expiring/revocable hashed sessions, workspace creation, role enforcement, origin checks, and cross-workspace tests. |
| Workspace-aware dashboard | Complete locally | Cookie-backed API requests, explicit workspace selection, sign-in/out controls, and first-workspace creation flow. JavaScript syntax check passes. |
| Custom webhook ingress and event history | Complete locally | Encrypted endpoint secrets, HMAC over timestamp/event ID/raw body, five-minute replay window, 256 KiB cap, workspace rate limiting, idempotency, secret rotation/revocation, cursor-paged metadata, and API tests. |
| Workspace credential storage | Complete locally | GitHub, Slack, and generic bearer secrets are Fernet-encrypted, workspace-scoped, idempotently created/rotated, revocable, redacted from API/audit output, and covered by PostgreSQL tests. No provider action consumes them yet. |
| Webhook workflow triggers | Complete in Demo Mode only | Immutable versions may match exact endpoint ID + JSON `type`; event intake and matching runs commit atomically. Replays do not enqueue twice, queue-full rolls intake back, and each run pins its selected version. Production workflow submission remains disabled. |
| GitHub/Slack integrations and production actions | Not started | No provider connections, webhook subscriptions, or production workflow action adapters exist. |
| Production secrets, scheduling, deployment, SDK/CLI | Incomplete | See `LIMITATIONS.md`. |

## Latest validation

- PostgreSQL integration suite: **31 passed** on PostgreSQL 18 / UTF-8, 2026-10-05. Includes OIDC configuration and claim validation, RBAC/tenant isolation, session revocation/expiry, signed webhook replay/rotation/revocation, encrypted credential create/rotate/revoke and cross-workspace isolation, trigger version pinning, queue rollback, cursor paging, fresh/upgrade migrations, concurrency, and SSE cursor reading.
- Ruff passes. The current `pytest-cov` run reports **79% app source coverage**; this is a report, not a configured threshold.
- `python -m compileall -q app tests benchmarks scripts` passes; the dashboard script passes `node --check`; `git diff --check` passes.
- Local live Demo Mode evidence: 10 duplicate deliveries -> 1 workflow -> 3 effects; a killed worker was replaced after lease expiry; DLQ replay completed. Queue depth ended at 0. The recorded P95 includes recovery delay.
- Synthetic 100-workflow P95: 3,096.99 ms (1 worker), 2,364.86 ms (2), 2,289.27 ms (4); 0/100 failures each. A local 20,000-row query measured 15.411 ms before and 0.126 ms after a partial index in one run. See `BENCHMARKS.md` and `docs/evidence.md`.
- The current verification did not run Docker, a live OIDC provider, external integrations, or a cloud deployment.

## Known limits and technical debt

- A configured OIDC provider and credentials are required for actual browser sign-in; only local claim/configuration behavior and cookie-backed sessions were tested here.
- Production API startup requires OIDC and a Fernet encryption key; static keys are Demo Mode only. Production workflow creation still returns 501 because available actions are sandbox-only.
- No GitHub/Slack OAuth lifecycle or provider webhooks, secure outbound HTTP action, or production workflow action adapter exists. Encrypted bearer credential storage is implemented, but stored credentials are not consumed. Production webhook events are stored and visible, but production workflow definitions/actions are still disabled; trigger execution currently dispatches only the sandbox action set in Demo Mode.
- Durable schedules, workflow conditions/branches, approvals, provider rate-limit handling, transactional external-action outbox, and production cancellation semantics remain open.
- Incoming webhook payloads have no retention/cleanup or endpoint-specific schema. The encryption master key has no automated rotation; public deployment also needs edge IP/network limits.
- PostgreSQL is the source of truth and queue. The per-tenant event-order lock can bottleneck high-volume writes. Independent broker scaling/replay has not been measured or justified.
- API and worker supervisor currently run together. Do not horizontally scale API supervisors with duplicate worker IDs; split workers and assign unique IDs first.
- OpenTelemetry traces, pool metrics, Grafana dashboards, alert rules, mypy/Pyright, a coverage threshold, dependency scanning, staging, infrastructure-as-code, rollback, and production deployment remain missing.
- The public repository still needs the remaining integration/deployment milestones before this project can be called complete.

## Latest change

Added OIDC account sessions and PostgreSQL workspace membership; protected routes resolve the selected workspace and role on every request. The dashboard uses session cookies, first-workspace setup, workspace selection, and a payload-free incoming-event history. Signed webhook endpoints support encrypted secrets, rotation/revocation, bounded JSON intake, timestamp validation, durable duplicate detection, and cursor pagination. Owners/admins can manage encrypted, workspace-scoped provider bearer credentials with idempotent create/rotation and revocation; no action adapter consumes them yet. Validation errors omit submitted values to prevent accidental credential echo. Demo Mode can match an exact event type to an immutable workflow version and queue it in the intake transaction. Seven numbered migrations are tested on fresh and upgraded schemas. OIDC provider interoperability, real actions, and production release remain unverified/incomplete.

## Next milestone

Implement a constrained outbound HTTP action that resolves encrypted workspace credentials, blocks SSRF and unsafe redirects, bounds request/response size and time, and passes stable idempotency keys. Keep production workflow submission disabled until its failure semantics are verified.
