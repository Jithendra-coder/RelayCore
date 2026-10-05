# Existing system audit

Audit snapshot: 2026-10-05, repository commit `f11e5da` plus the in-progress constrained HTTP action. Scope: application source, migrations, tests, CI, local deployment files, and repository documentation. No live identity provider, third-party API, cloud environment, or Docker daemon was available for inspection.

## A. What exists

- FastAPI API backed by PostgreSQL; database tables hold definitions, immutable workflow versions, runs, tasks, leases, audit events, incoming webhooks, and dead letters.
- Worker subprocesses claim work with PostgreSQL row locks, renew leases, retry failures, and recover expired work. The API supervisor currently also manages workers.
- Production OIDC sessions and workspace membership/roles; Demo Mode also exposes fixed API-key tenants and simulated actions.
- Signed custom webhook intake with encrypted rotating secrets, raw-body HMAC verification, event deduplication, bounded payloads, and metadata history. Exact-match workflow triggers currently execute only in Demo Mode.
- Workspace-scoped encrypted credentials and a constrained outbound HTTP action. Production workflows can be created and manually run when every step uses an HTTP credential bound to one operator-allowlisted host.
- Browser dashboard, polling-backed SSE, Prometheus text metrics, Docker Compose files, CI checks, operational docs, local benchmark and recovery evidence.

## B. Strong parts to preserve

- PostgreSQL is the queue and source of truth; admission, workflow creation, step progress, effects, retries, cancellation, and audit updates use explicit transactions.
- `FOR UPDATE SKIP LOCKED`, expiring leases, heartbeats, bounded retries, and process/database failure tests form a coherent recovery path.
- Immutable version rows and run snapshots prevent publishing a new workflow version from changing an existing run.
- Tenant IDs are resolved from authenticated membership; negative tests cover cross-workspace access.
- Event deduplication, idempotency constraints, queue bounds, secret encryption/redaction, and webhook freshness checks are tested at their trust boundaries.
- The current local evidence is measured and labeled synthetic where appropriate; the project does not claim exactly-once delivery or cloud scale.

## C. Incomplete or weak areas

- GitHub and Slack credential rows are storage only; there is no OAuth lifecycle, provider webhook registration/verification, event normalization, or provider action.
- Production webhook intake stores events but does not start workflows. HTTP bodies are static; event and prior-step values cannot yet be mapped into an action.
- There is no durable scheduler, condition/branch/wait/approval control flow, or provider-aware rate-limit policy.
- API and worker lifecycle are coupled, so horizontal API replicas could duplicate worker identities. No staged or cloud deployment exists.
- The UI is an operational dashboard, not a complete connection/workflow/execution/DLQ/audit product surface. There is no SDK or CLI.
- DNS lookup can outlive the HTTP socket timeout and worker lease. External delivery is at least once and depends on a provider honoring the idempotency key.
- Webhook body retention is unbounded; encryption-key rotation, edge IP limits, tracing, alerts, dependency scanning, live provider tests, and browser end-to-end tests are absent.

## D. Demo-only functionality

| Component | Decision | Reason |
|---|---|---|
| Fixed `record`/`charge` database effects | MOVE TO SANDBOX | Useful for local recovery demonstrations; they are not provider actions. Keep the production action allow-list separate. |
| `sleep`, `fail_once`, and `fail_until_replay` actions | MOVE TO SANDBOX | Deterministic controls for lease, retry, and DLQ evidence only. |
| Demo API keys and generated duplicate/DLQ endpoints | KEEP | Convenient local sandbox entry points; startup and route guards keep them out of production mode. |
| Synthetic benchmark workload and captured local evidence | KEEP | Valuable when clearly labeled as local/synthetic and never presented as provider-backed. |
| PostgreSQL workflow engine, OIDC, webhook intake, HTTP transport, and workspace authorization | KEEP | These are shared product foundations, not display-only fixtures. |

## E. Prioritized technical debt

- **P0 — correctness/security:** no known failing invariant in the audited local suite. External HTTP remains at-least-once; resolver stalls and provider idempotency behavior still need integration-level verification.
- **P1 — product/runtime:** production webhook dispatch and event mapping; real GitHub/Slack OAuth and provider paths; separate worker deployment identity; webhook retention; key rotation; production deployment and recovery controls.
- **P2 — operability/quality:** OpenTelemetry, alerting, type checking, dependency/security scanning, browser end-to-end coverage, provider contract tests, and a complete execution/connection UI.
- **P3 — developer experience:** typed Python SDK, API-backed CLI, richer workflow control flow, and a recruiter-facing case study/evidence pack.

## F. Rebuild decision

**Preserve and evolve the existing architecture.** The PostgreSQL workflow, lease, versioning, tenancy, webhook, and audit foundations are already coherent and have failure-oriented tests. Replace or isolate demo actions as production capabilities mature; do not rewrite the durable core or add a broker without measured evidence.
