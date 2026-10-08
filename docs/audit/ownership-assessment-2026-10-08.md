# RelayCore ownership audit and transformation plan

**Snapshot:** 2026-10-08

**Audited revision:** 0957c1fbb76542a8dc48069968714bf4becf9461 on main

**Target role:** Python Engineer

**Scope:** Repository source, tests, migrations, SDK/CLI, deployment, CI, operations, benchmarks, and current project evidence.

**Load assumed:** A self-hosted individual or small engineering team running intermittent repository events and workflow bursts. The checked-in queue limits are admission bounds; they are not capacity results.

What this repo does: RelayCore is a Python/FastAPI developer-event workflow engine backed by PostgreSQL. PostgreSQL stores immutable workflow versions, events, task leases, retries, and dead letters; separate Python workers execute a bounded set of HTTP and Slack actions. Current evidence proves local behavior and CI, not a live provider-backed or cloud deployment.

## Part A — Executive assessment

RelayCore has a real engineering core: it commits workflow admission and state in PostgreSQL, coordinates workers with row locks and leases, and tests duplicate delivery, recovery, restart, concurrency, and replay. It has grown beyond a tutorial or CRUD sample.

The actual product today is a locally verifiable, self-hostable workflow engine with production-oriented authentication and provider code whose third-party behavior is still unverified. Demo Mode is explicit and isolated. The default Compose stack is loopback-only.

**Architecture decision: Option A — incremental evolution.** Keep the PostgreSQL-backed modular monolith and its worker model. Fix the live-stream authorization gap, bound the DNS/action lease interaction, then prove one real external workflow in staging. There is no checked-in workload evidence that justifies Redis, Kafka, Kubernetes, or a full rebuild.

**Release position:** prototype. Do not describe it as production-ready until live identity/provider checks, a staging deployment, operational retention and recovery policies, and a real external-action failure test pass.

## Part B — Existing architecture

### Components and data flow

- FastAPI serves the dashboard, API, OIDC callbacks, GitHub and Slack integration callbacks, webhooks, metrics, and server-sent events (SSE).
- PostgreSQL is both the durable source of truth and work queue. Numbered migrations create identities, workspaces, workflow definitions and versions, webhook inbox, tasks, leases, side effects, schedules, tokens, events, and dead letters.
- API requests validate and authorize input, then write through Psycopg in explicit transactions. Worker processes claim ready tasks using PostgreSQL row locks with SKIP LOCKED. The coordinator recovers expired leases, dispatches due interval schedules, and clears expired webhook payloads.
- Published definitions are immutable versions. A run pins a definition, version, and hash. Signed events and matching triggered runs are persisted atomically.
- Production actions are bounded HTTPS requests to exact operator-approved hosts and fixed Slack messages. Demo-only actions are isolated in app/sandbox.py and are rejected in production mode.
- A Python SDK and relaycore CLI use workspace-bound bearer tokens. The SDK uses the standard library and refuses remote plaintext HTTP and redirects.

### Current user journey

**Local demo:** start the loopback Docker Compose stack; use a demo admin or viewer key; run the deterministic sample; kill a worker while it owns a lease; observe retry and recovery; send duplicate business events; create and replay a dead letter.

**Production-shaped path:** sign in with OIDC; create/select a workspace; connect GitHub or Slack, register a signed webhook, or store an HTTP credential; create and publish a definition through the API, SDK, or CLI; start it manually or let an exact webhook type or interval schedule start it; inspect persisted status and events in the dashboard or API.

The product dashboard supports identity/workspace administration, integrations, API-token management, run/event inspection, and the demo. It is not a general visual workflow editor. Live provider setup is not available in this workspace, and provider tests mock responses.

### Current production readiness

The repository provides deployable containers and a local Compose stack, a separate worker process role, logical backup/restore scripts, offline encryption-key rotation, telemetry code, Prometheus metrics/rules, and CI. These are useful building blocks. There is no staging or production environment, live provider verification, cloud deployment, migration rollout/rollback automation, managed backup-retention evidence, deployed alerting/trace collector, or hosted SDK release.

## Part C — Capability inventory

| Classification | Capability | Evidence and boundary |
|---|---|---|
| REAL locally | Durable workflow execution | PostgreSQL tasks, immutable versions, bounded admission, leases, retries, cancellation, dead-letter handling, replay, and persisted history are exercised by integration tests. |
| REAL in implementation; provider unverified | Identity and integrations | Authlib OIDC, GitHub App installation/webhook paths, Slack OAuth/events, signed custom webhooks, HTTPS actions, and Slack posting exist. Tests mock external responses; no live credentials were used. |
| REAL locally | Workspace security controls | Membership-derived access, role checks, origin protection for cookie mutations, hashed sessions/tokens, encrypted credentials, host-bound HTTP credentials, and cross-workspace tests exist. Deployment configuration has not been independently reviewed. |
| REAL locally | SDK and CLI | Typed standard-library client and relaycore commands cover workspace/definition/run operations. CLI smoke, API behavior, auth, idempotency, and redirect rejection pass. There is no package-index release. |
| PARTIAL | Production operations | Restore scripts pass a disposable PostgreSQL 16 CI drill; CI builds the image and validates Compose/Prometheus. Managed backup retention, live rollback, cloud recovery, and runtime telemetry delivery are not proven. |
| MOCKED | Provider behavior | GitHub, Slack, OIDC, and outbound provider transports are tested locally through mocks or controlled transports, not live provider accounts. |
| SIMULATED | Performance and failures | The load test uses synthetic database-only workflows. Worker failure, duplicate events, and deterministic failure injection are controlled test scenarios. |
| DEMO-ONLY | Payment/inventory/shipment narrative | The charge action writes a local effect record. It does not charge money or connect to an inventory or shipping provider. |
| BROKEN | Confirmed current defect | A long-lived SSE connection can continue returning tenant event metadata after the initiating session/token or workspace membership is revoked. See finding 1. |
| PLANNED-ONLY / absent | Product and operating features | Live provider verification, staging/production, a visual workflow editor, cron/time-zone schedules, fine-grained API-token scopes, provider reconciliation, managed retention, and hosted SDK distribution are absent. |

## Part D — Demo and mock inventory

- Demo Mode uses static keys and a deterministic tenant. Its allowed actions include recording local effects, delays, and controlled failures. It is a useful reliability demonstration, not a real business transaction.
- The benchmark creates generated workflows and uses database-only record effects. It cannot establish external-provider capacity or production throughput.
- Provider request/response behavior is mocked. This proves local validation and state transitions, not provider configuration, permissions, quotas, deployment DNS, or real delivery.
- Prometheus alert rules include firing/quiet tests in CI. No Prometheus server or Alertmanager has been deployed to deliver an alert.
- OTLP/HTTP protobuf export has a loopback receiver test. No OpenTelemetry Collector or trace backend has been deployed.
- Restore evidence uses disposable CI PostgreSQL instances. It is not a managed backup-retention or disaster-recovery test.

The project labels these boundaries honestly in README.md, PROJECT_STATUS.md, LIMITATIONS.md, BENCHMARKS.md, and docs/evidence.md. Keep those labels.

## Part E — Technical debt and risk ranking

### P0 — Must fix before shared production use

1. **Open SSE streams outlive authorization changes** — app/main.py:1723-1737. Authorization and workspace membership are resolved when the request starts. The stream then polls indefinitely without rechecking session validity, token revocation, or membership. If an admin removes a user while the browser remains connected, that connection can still receive future workspace event metadata. Close streams when the credential/membership ceases to be valid, or enforce a short reauthorization window; add an integration test that revokes access while streaming.

2. **DNS lookup is outside the configured HTTP timeout and worker lease** — app/http_action.py:191-207, 220 onward; app/worker.py:74-92; app/settings.py:19. The action timeout bounds socket connect/read work, but socket.getaddrinfo has no deadline. If the OS resolver stalls longer than the task lease, the coordinator can requeue the step while the original worker is still waiting; both attempts may then perform the external action. Use a bounded resolver/egress proxy or a heartbeat/lease strategy that covers resolution, and test a stalled resolver with a short lease. Preserve the documented at-least-once contract and require downstream idempotency where available.

3. **No provider-backed release gate exists yet** — docs/DEPLOYMENT.md and LIMITATIONS.md. No live OIDC, GitHub, or Slack credentials or staging endpoint are configured. Mocked integration tests cannot prove callback URLs, installed scopes, external signatures, provider retries, or secrets/network settings. Keep public release claims blocked until one isolated end-to-end provider workflow and its failure path pass in staging.

### P1 — Major security and operations work

4. **Database TLS is an operator requirement but not checked at startup** — app/settings.py:11 and app/main.py:224-230 pass DATABASE_URL directly into the pool. A misconfigured production URL can connect without certificate verification. Require TLS with hostname verification in the deployment contract and, preferably, reject insecure production DSNs; test both accepted and rejected configurations.

5. **Webhook metadata has no expiry or deletion policy** — app/store.py:1041 onward clears the raw body and parsed JSON, but event hashes, IDs, type, dedupe keys, and history remain indefinitely. Database backups and WAL may retain earlier bodies after live-row cleanup. Pick a tenant/legal retention policy, define deletion of metadata and backup/WAL expiry, then test the full lifecycle. The current code behavior is useful for dedupe but is not a complete retention policy.

6. **External delivery can repeat after an ambiguous response** — app/store.py execution path and docs/LIMITATIONS.md. A worker may lose its lease or process after the provider accepts a request but before the result is committed. HTTP sends a stable idempotency key, but the destination must honor it. Slack does not provide a dedupe key in this action, so duplicate posts are possible. Keep this limitation visible; before using non-idempotent actions, add a provider receipt/reconciliation or approval strategy.

7. **Workspace API tokens inherit the owner's whole current role** — docs/adr/011-workspace-api-tokens.md and app/auth.py:88-117. A leaked admin token can manage the workspace until revoked or expired, up to 90 days. Hash-only storage, one-time display, live membership checks, and revocation are strong controls; shorter defaults or fine-grained scopes would reduce the blast radius before wider adoption.

### P2 — Valuable improvements after the release blockers

8. **No browser end-to-end journey is automated.** API/database coverage is substantial, but it does not exercise login through the dashboard, workspace switching, token reveal/copy, live event updates, and reconnect behavior in a browser. Add one browser test when a staging identity provider is available.

9. **Type checking and container/OS image scanning are absent.** Ruff and dependency advisory scanning run in CI; a mypy/Pyright check and image/SBOM scan would catch different error classes. Add them as CI gates before production deployment, not as substitutes for live security review.

10. **The API module has several product responsibilities in one file.** app/main.py contains the routes for identity, token/credential administration, provider callbacks, webhooks, workflow/schedule control, SSE, and metrics. This is still a modular monolith, not an architecture defect by itself; after the auth/SSE work, move route groups only where it improves test isolation and ownership. Do not split solely to reduce line count.

### P3 — Optional, only with validated user need

- Cron and time-zone calendar schedules; conditional branches, approvals, waits, and compensation.
- Fine-grained token scopes if real SDK users need delegation narrower than current workspace roles.
- More provider actions, richer dashboard editing, multiple regions, an external broker, and deeper queue partitioning.
- AI/ML features. They do not improve the current workflow engine's core correctness or user problem.

## Part F — Capability expansion opportunities

1. Prove one narrow real workflow: a GitHub pull-request event starts a version-pinned workflow, sends a bounded HTTP request, and posts a Slack result. Record the provider event, queue/run trace, retries, and duplicate handling.
2. Turn provider uncertainty into a repeatable staging contract test: real test workspace/app, callback validation, delivery signature, permissions, rate-limit response, uninstall/revoke, and safe cleanup.
3. Improve end-to-end action semantics only where providers support it: stable idempotency, provider request IDs, receipts, retry classification, and reconciliation for ambiguous completion.
4. Make the control plane safe to operate: auth revocation during SSE, enforce database TLS, define metadata/backup retention, and exercise restore and key rotation with the same deployment configuration.
5. Add operational visibility to the deployed system: API/worker/database health, queue age, failed actions, provider latency, and alert delivery.
6. Improve the developer path with the existing SDK/CLI and a real release runbook before building a large visual editor.

Avoid feature-count growth. Redis/Kafka, Kubernetes, AI agents, and connector catalogs do not solve an evidenced current bottleneck.

## Part G — Market and interview value

Scores are judgment calls based on code evidence, not measured outcomes.

| Area | Score | Why; what moves it higher |
|---|---:|---|
| Python engineering relevance | 9/10 | Real service, typed models/SDK, database transactions, workers, API, integrations, and tests. Add live deployment and operation evidence. |
| Engineering depth | 8/10 | Durable state, leases, recovery, idempotency, and integration security are interview-worthy. Close the DNS/lease and live-stream reauthorization gaps. |
| Reliability evidence | 8/10 locally; 4/10 in production | Failure and restore tests exist, but only local/disposable environments are proven. Run the same drills in staging. |
| Technical distinctiveness | 7/10 | PostgreSQL as a durable queue with immutable versions is a defensible core; a generic “automation platform” pitch weakens it. Show a real developer event workflow. |
| User usefulness | 6/10 | The use case is useful to engineering teams, but live interoperability and a complete authoring journey are missing. Make one release/incident workflow excellent. |
| Security maturity | 7/10 in source; not production-reviewed | Strong tenant, secret, webhook, and egress controls; stream reauthorization and operator lifecycle remain. |
| Operational maturity | 5/10 | Runbooks, metrics, alerts, backups, rotation, and CI exist; no deployed staging, managed backup policy, or live alert/trace delivery. |
| Deployability | 4/10 | Local containerization is credible; no infrastructure, staging, cloud release, migration rollback, or live smoke test. |
| Evidence quality | 7/10 | Repeatable test/benchmark/restore evidence is stored and measured honestly. Add staging traces and provider failure evidence. |

A recruiter should remember: “A PostgreSQL-backed workflow engine that survives worker/database interruption and makes duplicate-delivery limits explicit.” A senior engineer should ask about transaction boundaries, leases, PostgreSQL queue trade-offs, idempotency, and external side-effect ambiguity.

## Part H — Keep / improve / refactor / replace / remove

| Subsystem | Decision | Reason |
|---|---|---|
| PostgreSQL state machine and queue | KEEP | Durable, tested core; no measured reason to add a broker. |
| Immutable workflow versions and run snapshots | KEEP | Gives replay/history stable semantics. |
| Worker leases, coordinator, recovery, DLQ | IMPROVE | Strong base; cover resolver stalls and external-action ambiguity. |
| Workspace identity and role checks | IMPROVE | Preserve OIDC/RBAC model; revalidate long-lived streams and harden deployed DB TLS. |
| Webhook inbox and event matching | KEEP | Signed, deduplicated, transactionally triggers runs. Complete metadata retention policy. |
| HTTP action | IMPROVE | Bounded host/TLS/response controls are good; DNS needs bounded behavior within lease. |
| Slack/GitHub integration | IMPROVE | Preserve the shared event path; validate against live provider accounts. |
| API tokens / SDK / CLI | KEEP | Useful developer access path; reduce delegated authority when real need is proven. |
| FastAPI modular monolith | KEEP | Suitable for current scale and simplifies transactions. Split route modules only to improve test/ownership boundaries. |
| Demo sandbox | KEEP, clearly label | Reproducible failure demonstration; never represent its charge action as payment. |
| Dashboard | IMPROVE | Good operator visibility and controls; add browser E2E and guided real workflow, not decorative complexity. |
| Redis/Kafka/Kubernetes/AI | REMOVE from near-term plan | No measured or product requirement supports the cost today. |

## Part I — Final project vision

### What are we building?

A self-hostable developer event automation engine for a small engineering team. It accepts signed repository or custom events, runs immutable workflow versions durably, and performs tightly bounded HTTP or Slack actions with visible retries and failures.

### Who uses it, and why?

A platform or release engineer who wants repeatable pull-request, release, or incident runbooks without hiding execution state in a one-off webhook script. They need to see what ran, why it retried, what version ran, and whether replay is safe.

### What real problem does it solve?

Webhook handlers often acknowledge events and then lose work during process restart, duplicate delivery, or provider outage. RelayCore records the event and workflow transactionally, then coordinates recoverable background work.

### Why is the problem technically difficult?

PostgreSQL cannot atomically commit an unrelated Slack or HTTP provider side effect. Worker crashes and timeouts create ambiguous outcomes. Correctness depends on explicit lease ownership, deduplication, bounded retries, downstream idempotency, version pinning, tenant authorization, and truthful failure states.

### What makes it different?

Not the number of integrations. Its technical identity is the use of PostgreSQL as durable queue and workflow state, with immutable run snapshots, recovery, and explicit at-least-once external delivery semantics.

### Three to five differentiators

1. Transactional webhook intake and run admission.
2. PostgreSQL lease/claim/recovery path tested across process and database interruption.
3. Immutable workflow versions and durable history.
4. Tenant-scoped secrets and constrained outbound network access.
5. Honest duplicate/retry behavior rather than unsupported exactly-once claims.

### 30-second recruiter memory

“RelayCore turns real developer events into auditable, recoverable workflows and has tests for worker crashes, duplicate events, and database restarts.”

### Senior engineer discussion

Why PostgreSQL instead of a broker; how SKIP LOCKED and leases behave; which state changes share a transaction; when duplicates are possible; how an action can be reconciled; how webhook signatures and SSRF defenses work; and how deployments preserve schema and encryption keys.

## Part J — Uniqueness assessment

RelayCore is not a novel workflow product category. Its differentiator is engineering substance: a small PostgreSQL-centered durable execution core with explicit failure/recovery behavior and constrained provider actions. It passes the “not just another chatbot/CRUD app” test.

It risks looking generic if presented as a broad no-code automation or Zapier clone, if Demo Mode is mistaken for live payment, or if the README lists integrations without one live path. Narrow the message to developer release and incident runbooks, then show the real event-to-action history and failure case.

## Part K — Target architecture

Keep the current modular monolith and PostgreSQL queue for the assumed small-team workload. Deploy API and worker roles separately in staging/production; the API should use RELAYCORE_WORKERS=0 when horizontally replicated. Keep the coordinator's lease/schedule/cleanup work behind safe PostgreSQL locking. Add a broker only after staging measurements demonstrate database queue contention or independent consumer scaling needs.

Target flow:

Developer provider or signed webhook → authenticated/verified intake → transactional event + matching immutable run → PostgreSQL ready queue → leased worker → bounded provider action → persisted result/event → dashboard, SDK, traces, and alerts.

Retain the shared database schema as the source of truth. Add provider receipts/reconciliation only for actions where the provider offers useful identifiers or idempotency guarantees. Do not pretend a local outbox can make an unrelated provider transaction atomic.

## Part L — Final tech stack

| Technology | Keep? | Justification |
|---|---|---|
| Python 3.13 / FastAPI | Yes | Fits the existing API, workers, SDK, tests, and async request surface. |
| PostgreSQL 18 target | Yes | Durable state, transactions, queue claims, uniqueness, schedules, and event history in one source of truth. |
| Psycopg 3 | Yes | Explicit transaction and pool behavior already tested. |
| Authlib / OIDC | Yes | Real protocol implementation; complete provider-backed staging verification. |
| Fernet / cryptography | Yes, with operational key custody | At-rest credential encryption is implemented; rotate with backups and maintenance controls. |
| Standard-library SDK/CLI | Yes | Keeps the API client small and avoids a runtime dependency for simple HTTP calls. |
| Docker Compose | Yes for local development | Reproducible local stack; it is not a production deployment architecture by itself. |
| OpenTelemetry + Prometheus format | Yes | Useful correlation and queue/lease signals; deploy collector and alert delivery. |
| Redis, Kafka, Kubernetes, AI/ML | No for now | No measured need; they add operating burden without fixing current gaps. |

## Part M — Real integrations

| Integration | Purpose | Current evidence | Next proof |
|---|---|---|---|
| OIDC identity provider | Human login and workspace identity | Authlib flow and claim checks; no live issuer configured | Test login, verified claims, expiry/revocation, logout in staging. |
| GitHub App | Pull-request events | PKCE installation and signed webhook paths; provider responses mocked | Install a test App and verify a signed real delivery and revocation. |
| Slack App | App mentions and fixed message posts | OAuth/events/action logic tested with mocks | Verify test workspace scopes, signatures, rate limits, duplicates, and uninstall. |
| Custom signed webhooks | Bring in arbitrary developer events | HMAC, timestamp, size, dedupe, atomic trigger path tested locally | Test through deployed ingress/rate limits with a real sender. |
| Operator-approved HTTPS endpoint | Execute a downstream action | DNS/host restrictions and transport tested without network | Verify one idempotent test endpoint and timeout/duplicate handling. |
| PostgreSQL | Durable data and coordination | PostgreSQL 17.9 local tests, PostgreSQL 18 CI, restore drill | Run managed staging DB with TLS and restricted role. |

## Part N — Security architecture

### Existing controls

- Verified OIDC issuer/subject/email checks; opaque hashed application sessions; current workspace membership and role lookup.
- Cookie mutation origin checks; parameterized SQL; strict Pydantic input validation; fixed workflow action allow-list; no user Python/shell execution.
- High-entropy API tokens shown once and stored as SHA-256 hashes; workspace binding, expiry, current role checks, revocation.
- Fernet-encrypted webhook and provider credentials; webhook HMAC over timestamp, event ID, and raw body; freshness, payload-size, and dedupe controls.
- HTTPS-only HTTP action with exact host allowlist, public DNS check/address pinning, hostname TLS verification, no redirects, bounded response, and safe history.
- Sandbox action separation and production rejection; request IDs and structured failure logs omit exception contents and secrets.

### Security priorities

1. Close the open SSE authorization lifetime described in finding 1.
2. Bound DNS resolution and keep leases/action behavior consistent as described in finding 2.
3. Enforce verified database TLS in the production connection policy, not only in operator documentation.
4. Define webhook metadata, backup, and WAL retention together.
5. Keep API tokens short-lived; decide whether full-role delegation is acceptable before inviting external SDK users.
6. Add edge network/rate limits, secret-manager custody, container scanning, and a live security review before broad public ingress.

SSRF, XSS, CSRF, SQL injection, and prompt injection were reviewed at the source/design level. No AI or arbitrary-code execution path exists, so prompt-injection controls are not applicable.

## Part O — Reliability and failure behavior

| Failure | Current behavior | Remaining limit / expected target |
|---|---|---|
| Database unavailable | API/worker requests fail; worker reconnect loop and recovery tests cover interruption. | Stage with managed failover and test accepted work after recovery. |
| Worker exits or lease expires | Coordinator retries with bounded backoff or dead-letters; another worker may claim. | Include resolver stalls and long provider delays in lease tests. |
| Duplicate input | Unique event/idempotency constraints avoid duplicate runs for the same key/payload; mismatched payload conflicts. | Keep key-retention behavior aligned with metadata cleanup. |
| Queue full / rate limit | Admission returns a bounded error rather than growing an in-memory backlog. | Measure client retry/backoff behavior in staging. |
| HTTP provider timeout/5xx/429 | Retryable classification; stable idempotency key; bounded response; permanent failures dead-letter. | Destination must honor idempotency; DNS can exceed configured timeout. |
| Slack 429/provider error | Retry delay is persisted where available; permanent errors dead-letter. | Slack message may duplicate after accepted request and lost response. |
| Cancellation during provider call | New steps stop; in-flight action may finish and its result is recorded after cancel. | Expose this terminal state clearly and verify it with live providers. |
| Event payload expires | Bodies clear in batches; nonterminal runs protected; expired event-dependent DLQ replay is rejected. | Metadata and backups remain until a product policy is chosen. |
| Deployment/migration failure | Migration scripts are ordered and transaction-locked; restore drill is tested. | No production expand/contract rollout or rollback automation. |
| Telemetry backend outage | Core work is not supposed to depend on telemetry delivery. | Verify exporter timeouts/queue behavior against deployed collector. |

At-least-once is the correct honest contract for external work. Effectively-once applies only to database side effects or provider operations honoring a stable idempotency key.

## Part P — Observability

Already implemented: request ID propagation, worker/run/task/attempt identifiers in events/logs, optional W3C trace context through durable tasks, OTLP/HTTP spans, tenant-authenticated Prometheus gauges, and sample alert rules. CI validates Prometheus syntax and firing/quiet conditions.

Still needed: deployed collector and trace backend; deployed Prometheus/Alertmanager and one verified alert delivery; database pool/lock/connection metrics; provider latency and outcome metrics; deployment and migration version labels; dashboards for queue age, expired leases, retries, DLQ, and per-provider failures. Never add workflow payloads, bearer values, secrets, or exception messages to spans/logs.

Useful correlation chain: request_id → webhook event ID → workflow run ID and immutable version → task ID → worker ID and attempt → provider request ID (when safely available).

## Part Q — Testing and evaluation audit

### Current coverage

- PostgreSQL integration tests cover transactions, migrations, idempotency, queue saturation, concurrent dispatch, versions, tenancy/RBAC, webhook verification/expiry, OAuth state, credentials, SDK/CLI, action classification, worker recovery, service restart, secret rotation, telemetry, and restore-drill guardrails.
- CI runs Ruff, pre-commit, pytest with an 80% app-source coverage floor, both Compose config checks, Prometheus rule/config tests, a Docker image build, a PostgreSQL 16 backup/restore job, and a Python dependency audit.
- There are real child-process and PostgreSQL restart tests, not only mocked unit tests.

### Gaps

- No browser E2E from login through workspace selection and live dashboard stream.
- No live provider contract test or deployed smoke test.
- No test revokes membership/session/token during an open SSE stream.
- No bounded-resolver/short-lease duplicate test.
- No production load or resource benchmark, property-based suite, static type check, image/OS scan, or deployed alert/collector test.

Tests that matter next: streaming authorization revocation; DNS stall while a leased task is active; real provider happy path plus duplicate/429/outage path; staging migrations/restore; and browser smoke once an OIDC test provider exists. Keep synthetic results labeled as simulated or local measured data.

## Part R — Performance and benchmark plan

### Evidence already measured

- Local synthetic workload: 100 two-step database-only workflows; P95 3,096.99 ms with one worker, 2,364.86 ms with two, and 2,289.27 ms with four; 0/100 failures in each recorded scenario.
- One local 20,000-row queue-shaped query measured 15.411 ms before a partial index and 0.126 ms after.
- PostgreSQL 18 CI suite and PostgreSQL 16 disposable restore drill pass. These are correctness/verification evidence, not capacity claims.

### Measure before optimizing

In staging, record hardware/instance sizes, DB version, action mix, event rate and burst shape. Measure end-to-end throughput, queue wait, action time, P50/P95/P99 latency, retry and duplicate rates, DB pool wait/locks/query time, worker CPU/RAM, queue age, and provider rate-limit responses. Run one-worker, normal, and burst cases with realistic provider latency and at least two tenants.

The likely first constraints are PostgreSQL contention/connection budget, serialized event ordering per tenant, worker poll/lease churn, and external-provider latency. Only introduce a broker or partitioning after a repeatable measurement demonstrates that PostgreSQL is the limiting factor.

## Part S — Deployment plan

### Local

Use the documented loopback-only Demo Mode Compose stack. Keep the demo keys and fake charge action clearly labeled. Do not expose this configuration to the public internet.

### Staging

- Provision API and worker as separate roles, managed PostgreSQL with TLS verification and least-privilege credentials, a secret manager, restricted egress/ingress, and an OIDC test issuer.
- Use test GitHub/Slack apps and a dedicated workspace. Configure backups and retention, deploy Prometheus/Alertmanager and an OTLP collector.
- Apply migrations with an explicit deployment step; smoke-test login, webhook intake, one workflow, one worker kill/recovery, one restore, one alert, and one trace.
- Exercise rollback and key rotation before storing meaningful user credentials.

### Production

Deploy only after staging acceptance. Pin an image digest; use managed backups with tested retention; define migration compatibility and rollback; monitor queue age/leases/DLQ/provider health; keep API and workers separately scalable. Kubernetes is not required for one API plus a small worker pool.

## Part T — Cost strategy

No cloud provider or deployment size is selected, so a dollar estimate would be invented. Cost drivers are always-on API/worker compute, managed PostgreSQL storage/IO/backup/WAL, telemetry retention, and egress. There are no model API, GPU, vector database, or broker costs.

Start with one modest API instance, one or two workers, and a small managed PostgreSQL instance with explicit backup/telemetry retention. Record actual utilization and event volume. Scale worker count first if queue age rises while the database has headroom; tune PostgreSQL or add a broker only from measured saturation.

## Part U — Transformation roadmap

### Phase 1 — Close authorization and action-lease gaps

- **Objective:** ensure revoked users stop receiving stream data; prevent unbounded DNS delay from silently exceeding task lease assumptions.
- **Existing code:** app/main.py SSE route and identity dependencies; app/http_action.py DNS/HTTP path; worker lease heartbeat/recovery; tests/test_workflows.py and tests/test_http_action.py.
- **Keep:** workspace scoping, pinned HTTPS, immutable workflow state, at-least-once contract.
- **Change:** periodically revalidate/terminate SSE sessions; put DNS under an explicit bounded strategy and align action execution with lease renewal.
- **Capability:** reliable revocation and bounded external request lifecycle.
- **Tests/security:** revoke session, API token, and membership mid-stream; stall resolver longer than lease; check whether any duplicate action occurs.
- **Observability:** log stream close reason and action timeout/lease outcome without payload or secret.
- **Acceptance/evidence:** no post-revocation events; resolver delay cannot cause uncontrolled overlapping action; tests pass on PostgreSQL 18 CI; record the exact failure scenario and result.

### Phase 2 — Prove one live developer workflow

- **Objective:** verify one real GitHub event → durable run → HTTP or Slack outcome using test accounts.
- **Existing code:** app/github.py, app/slack.py, app/main.py integration routes, event store, action execution, SDK/CLI.
- **Keep:** mock tests for edge cases and exact event/run transaction.
- **Change:** fix only interoperability issues found against provider sandboxes/test workspaces; keep permission scopes narrow.
- **Capability:** first real production-shaped user workflow.
- **Tests/security:** signature, OAuth state, callback URL, scope, duplicate event, revoked install, 429/outage, no secrets in history.
- **Observability:** provider outcome/request IDs, queue delay, retry and DLQ trace.
- **Acceptance/evidence:** staged run is reproducible; duplicate event starts only one run; retry/ambiguity is documented and visible; safe teardown works.

### Phase 3 — Establish an operable staging deployment

- **Objective:** move from local reproducibility to controlled deploy/recovery.
- **Existing code:** Dockerfile, Compose worker role, migrations, scripts/backup.ps1, scripts/restore.ps1, scripts/rotate_secrets.py, deployment/runbooks.
- **Keep:** non-root container, separate worker mode, tested restore and offline key rotation.
- **Change:** enforce verified database TLS; add image digest release, migration compatibility/rollback process, secret manager configuration, external backups/WAL retention, health/readiness policy, and ingress limits.
- **Capability:** safe staged upgrade and recoverable service operation.
- **Tests/security:** configuration rejection for insecure DB DSN; restore to clean target; rotation with previous backup key; worker drain/restart; ingress cap/rate tests.
- **Observability:** deployed alerts and traces; attach release/schema versions to logs.
- **Acceptance/evidence:** staging deploy, smoke, migration, restore, rollback, alert, trace, and key-rotation runbooks all executed and retained.

### Phase 4 — Set data and token lifecycle policy

- **Objective:** make data persistence and access delegation suitable for real workspaces.
- **Existing code:** incoming_events cleanup, audit/events schema, API-token model and dashboard.
- **Keep:** hash-only token storage and payload expiry safeguards.
- **Change:** define metadata, backup, and WAL deletion periods; consider shorter default API-token expiry or scopes if users need narrow automation identities.
- **Capability:** documented workspace data lifecycle and reduced credential blast radius.
- **Tests/security:** expiry/deletion tests across events, dedupe, history, backups, and active runs; token revocation/role downgrade and least-privilege cases.
- **Observability:** report expired/deleted rows and token lifecycle events without secrets.
- **Acceptance/evidence:** published retention schedule and verified cleanup/backup expiry behavior; token policy documented with actual role semantics.

### Phase 5 — Make the proof usable and measure its limit

- **Objective:** let another developer reproduce a real workflow and inspect its reliability evidence.
- **Existing code:** dashboard, SDK/CLI, BENCHMARKS.md, docs/evidence.md, runbooks.
- **Keep:** concise dashboard, SDK, CLI, benchmark methodology.
- **Change:** add one browser smoke path and a guided real workflow; rerun measured staging benchmarks with recorded resource sizes.
- **Capability:** reproducible recruiter/user experience and honest capacity envelope.
- **Tests/security:** browser auth/workspace/action flow; accessibility smoke; no token in storage/logs; two-tenant burst.
- **Observability:** export benchmark conditions and trace/event identifiers.
- **Acceptance/evidence:** a clean setup reaches one real run, failure/recovery evidence, and an explicitly scoped benchmark without manual code edits.

## Part V — Acceptance criteria

A phase is complete only when its acceptance checks pass in CI or the target staging environment and the evidence is saved:

1. No stream delivers events after session, token, or membership revocation.
2. Resolver/action timeouts cannot outlive the chosen lease policy without an explicit in-flight state and idempotency outcome.
3. One real signed provider delivery triggers one immutable workflow in staging.
4. Staging verifies TLS, deployment roles, migrations, backup/restore, rollback, key rotation, alert delivery, and trace export.
5. Retention includes live rows plus backups/WAL and has an owner-approved policy.
6. A first-time developer reproduces the real workflow and sees the real limits.
7. All claims distinguish MEASURED, SIMULATED, ESTIMATED, and ASSUMED.

## Part W — Evidence strategy

Store permanent evidence under docs/evidence/ and benchmarks/results/:

- CI run links, Python/PostgreSQL versions, test count, coverage, and advisory scan result.
- A live test-provider workflow trace with IDs redacted where needed; event signature/dedupe outcome and provider request identifiers.
- Worker-kill, database restart, resolver stall, duplicate/429, DLQ/replay, and cancellation records.
- Staging deployment digest, migration result, backup/restore verification, key-rotation evidence, alert firing/recovery, collector trace.
- Benchmark raw output with workload mix, hardware, database size/version, worker count, concurrency, P50/P95/P99, failures, queue wait, CPU/RAM, and provider latency.
- Architecture, threat model, retention policy, and operational decision records.

Never convert local synthetic measurements into production claims.

## Part X — Portfolio presentation

Lead with the live product flow, not a technology list:

1. Show a real test GitHub event entering RelayCore.
2. Show its immutable workflow version, worker attempt, and persisted result.
3. Kill a worker or force a provider retry and show recovery/DLQ honestly.
4. Show a trace/alert and benchmark conditions.
5. Link architecture decisions, security limits, tests, source, and deployment status.

State clearly that Demo Mode simulates business effects. Include the at-least-once contract and the cases where duplicates can happen. A short clear case study is stronger than a polished but non-live feature collage.

## Part Y — Interview discussion map

The project can support a grounded 30-minute discussion on:

- PostgreSQL queue vs external broker and the load evidence required to change.
- Transactions for webhook admission, immutable version pinning, and state transitions.
- SKIP LOCKED, lease heartbeats, expired work, idempotency, and replay.
- Why exactly-once external actions cannot be promised without provider cooperation.
- HTTP host validation, public DNS pinning, TLS, redirects, bounds, and DNS timeout limits.
- OIDC identity, workspace roles, token hashing, session revocation, and long-lived streams.
- HMAC input canonicalization, timestamp/replay defense, event dedupe, and retention.
- Deployment role boundaries, migration/backup/rotation runbooks, traces and queue-age alerts.
- Synthetic versus live benchmarks, limitations, cost drivers, and why AI/Redis/Kafka/Kubernetes were not added.

## Part Z — Final verdict

1. **Is the current foundation worth preserving?** Yes. PostgreSQL-backed durable execution is the strongest part.
2. **Should any subsystem be rebuilt?** No full rebuild. Fix SSE authorization lifetime and the bounded DNS/lease action boundary; evolve deployment and reconciliation selectively.
3. **Is the project unique enough?** It is technically credible, but not a novel product category. Its defensible identity is a tested PostgreSQL-centered developer workflow engine with failure evidence.
4. **Five highest-value improvements?** (a) close SSE revocation; (b) bound resolver/action behavior against lease expiry; (c) complete one live provider flow; (d) deploy staging with verified DB TLS, migrations, restore, alerts, and traces; (e) define metadata/backup retention and token authority.
5. **What should not be added?** AI/ML, Redis/Kafka, Kubernetes, multi-region, connector sprawl, or a broad visual builder before actual user demand and measurement.
6. **What would make this exceptional?** One live GitHub-to-action workflow, tested under duplicate delivery, worker loss, provider outage, restore, and staged deployment, with evidence another engineer can reproduce.
7. **What would make it look generic?** Calling it a no-code automation clone, claiming exactly-once/production scale, or presenting simulated charge/payment effects as real integrations.
8. **What should be built first?** Fix stream reauthorization and resolver/lease behavior. Then use real test credentials to verify one end-to-end workflow.

## Validation performed for this audit

- Local full suite on the disposable PostgreSQL 17.9 database: **106 passed**, **81.51% app coverage** (80% floor).
- Current GitHub Actions at the audited revision: [tests and PostgreSQL 16 restore drill](https://github.com/Jithendra-coder/RelayCore/actions/runs/37783826558) passed; PostgreSQL 18 test job reports **106 passed, 82.37% app coverage**. [Dependency advisory audit](https://github.com/Jithendra-coder/RelayCore/actions/runs/37783826609) passed.
- Local Ruff and Python compileall passed; relaycore --help works. CI also passed pre-commit, editable package install, both Compose config validations, Prometheus config/rule tests, and Docker image build.
- Working tree was clean before this report. Runtime/application code was not changed for this audit.

## Not checked

No live OIDC/GitHub/Slack account, cloud environment, deployed Prometheus/Alertmanager/collector, browser E2E, production network, or managed-backup policy was available. Docker was unavailable locally; Compose/image checks were verified in CI. The source review focused on trust boundaries, persistence, workers, integrations, tests, and operations; it did not line-read every static CSS/HTML statement or each historical migration constraint.

## Remediation follow-up — 2026-10-08

The findings above describe audited revision `0957c1f`; the following follow-up is against the current working tree:

1. **SSE revocation:** each poll now validates current workspace membership and the backing session or API token. Regression tests revoke each credential/membership during an open stream and confirm it closes on the next poll.
2. **DNS and lease budget:** outbound HTTP and Slack actions use one total deadline across DNS, connection, and response handling. The worker renews the task lease immediately before the provider call and caps the action deadline at half the lease. DNS calls that cannot be cancelled are bounded by a four-slot semaphore; timeout/capacity failures do not open an outbound connection. Lease settings below 0.2 seconds are rejected.

The local PostgreSQL 17.9 suite passed **113 tests** at **81.84% app coverage**; Ruff, compileall, pre-commit, and `git diff --check` passed. GitHub Actions for the follow-up are pending push. Live providers and production deployment remain unverified; external effects still have at-least-once semantics.

