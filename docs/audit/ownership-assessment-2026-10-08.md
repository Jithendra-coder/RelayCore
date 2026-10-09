# RelayCore ownership audit and transformation plan

**Snapshot:** 2026-10-09

**Audited revision:** `ce2b62ca9a6c0a6a97530ee700083555fc1d6ce3` on main (documentation-only commit; runtime code at `20a77d060359e63b9a82aa6e3b222c2eb71c043f`)

**Target role:** Python Engineer

**Scope:** Repository source, tests, migrations, SDK/CLI, deployment, CI, operations, benchmarks, and current project evidence.

**Load assumed:** A self-hosted individual or small engineering team running intermittent repository events and workflow bursts. The checked-in queue limits are admission bounds; they are not capacity results.

What this repo does: RelayCore is a Python/FastAPI developer-event workflow engine backed by PostgreSQL. PostgreSQL stores immutable workflow versions, events, task leases, retries, and dead letters; separate Python workers execute a bounded set of HTTP and Slack actions. Current evidence proves local behavior and CI, not a live provider-backed or cloud deployment.

## Part A — Executive assessment

RelayCore has a real engineering core: it commits workflow admission and state in PostgreSQL, coordinates workers with row locks and leases, and tests duplicate delivery, recovery, restart, concurrency, and replay. It has grown beyond a tutorial or CRUD sample.

The actual product today is a locally verifiable, self-hostable workflow engine with production-oriented authentication and provider code whose third-party behavior is still unverified. Demo Mode is explicit and isolated. The default Compose stack is loopback-only.

**Architecture decision: Option A — incremental evolution.** Keep the PostgreSQL-backed modular monolith and its worker model. The previously identified SSE revocation and DNS/action lease issues have follow-up fixes and regression tests in the current tree. The next meaningful proof is one real external workflow in staging, followed by an explicit retention policy and managed recovery evidence. There is no checked-in workload evidence that justifies Redis, Kafka, Kubernetes, or a full rebuild.

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
| REAL locally | Authorization revocation | SSE polls revalidate workspace membership and the backing session/API token; regression tests cover revocation during an open stream. Live multi-user deployment behavior remains unverified. |
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

1. **No provider-backed release gate exists yet** — docs/DEPLOYMENT.md and LIMITATIONS.md. This environment has no configured live OIDC, GitHub App, Slack App, or staging endpoint. Mocked provider tests cannot prove callback URLs, installed scopes, external signatures, provider retries, or deployment network/secret settings. Keep production-readiness claims gated on one isolated end-to-end provider workflow and its failure path in staging.

### P1 — Major security and operations work

2. **Webhook metadata and backup/WAL retention have no complete deletion policy.** Raw bodies and parsed JSON expire, but event hashes, IDs, types, dedupe keys, and history remain. Backups and WAL may outlive live-row cleanup. Choose a tenant/legal retention period and define backup/WAL expiry before real customer payloads are stored.

3. **External action outcomes remain ambiguous after a lost response.** A provider may accept an action while RelayCore loses the response or crashes before recording it. HTTP sends a stable idempotency key, but only the target can enforce it; Slack messages and GitHub comments can duplicate after ambiguity. Keep the at-least-once contract explicit and add provider receipts/reconciliation only where a real provider supports them.

4. **No managed staging/recovery operation is evidenced.** CI validates container setup and a disposable PostgreSQL restore, but there is no managed deployment, backup retention, migration rollback drill, deployed alert delivery, or collector/trace backend. Complete a controlled staging run before shared production use.

### Closed findings verified in the current tree

- **SSE revocation:** each poll checks current membership and the backing session/API token; integration tests close streams after session, token, and membership revocation.
- **DNS/action lease budget:** DNS uses a timeout and bounded resolver slots; action deadlines are capped at half the task lease with heartbeats around provider calls. Tests cover resolver stalls, no outbound connection after timeout, and shared DNS/HTTPS timeout budget. A timed-out OS resolver thread itself cannot be cancelled, but its concurrency is bounded.
- **Database TLS:** production API, worker, and rotation paths require PostgreSQL `sslmode=verify-full`; staging certificate trust remains unverified.
- **New API-token authority:** new tokens have a role ceiling and are capped by current membership. Existing tokens retain their migrated authority; narrower scopes remain optional.
- **Static and image checks:** Mypy covers 24 modules; CI runs container image scanning and Python dependency audit.

### P2 — Valuable improvements after the release blockers

5. **No complete browser user journey is automated.** CI has a browser dashboard/separate-worker smoke, but not OIDC login through workspace selection, token operations, live event updates, and reconnect behavior. Add that journey when a staging identity provider is available.

6. **The API module has several product responsibilities in one file.** app/main.py contains identity, token/credential administration, provider callbacks, webhooks, workflow/schedule control, SSE, and metrics. It is still a modular monolith, not an architecture defect by itself; split routes only when it improves test isolation or ownership.

7. **Independent production security review and edge controls remain.** Add deployed ingress limits, secret-manager custody, and environment-specific review before exposing provider callbacks publicly.

### P3 — Optional, only with validated user need

- Cron and time-zone calendar schedules; conditional branches, approvals, waits, and compensation.
- Fine-grained token scopes if real SDK users need delegation narrower than current workspace roles.
- More provider actions, richer dashboard editing, multiple regions, an external broker, and deeper queue partitioning.
- AI/ML features. They do not improve the current workflow engine's core correctness or user problem.

## Part F — Capability expansion opportunities

1. Prove one narrow real workflow: a GitHub pull-request event starts a version-pinned workflow, sends a bounded HTTP request, and posts a Slack result. Record the provider event, queue/run trace, retries, and duplicate handling.
2. Turn provider uncertainty into a repeatable staging contract test: real test workspace/app, callback validation, delivery signature, permissions, rate-limit response, uninstall/revoke, and safe cleanup.
3. Improve end-to-end action semantics only where providers support it: stable idempotency, provider request IDs, receipts, retry classification, and reconciliation for ambiguous completion.
4. Make the control plane safe to operate: select metadata/backup retention, then exercise restore, key rotation, alert delivery, and traces with the staging deployment configuration.
5. Add operational visibility to the deployed system: API/worker/database health, queue age, failed actions, provider latency, and alert delivery.
6. Improve the developer path with the existing SDK/CLI and a real release runbook before building a large visual editor.

Avoid feature-count growth. Redis/Kafka, Kubernetes, AI agents, and connector catalogs do not solve an evidenced current bottleneck.

## Part G — Market and interview value

Scores are judgment calls based on code evidence, not measured outcomes.

| Area | Score | Why; what moves it higher |
|---|---:|---|
| Python engineering relevance | 9/10 | Real service, typed models/SDK, database transactions, workers, API, integrations, and tests. Add live deployment and operation evidence. |
| Engineering depth | 8/10 | Durable state, leases, recovery, idempotency, and integration security are interview-worthy. Add live provider and managed staging evidence. |
| Reliability evidence | 8/10 locally; 4/10 in production | Failure and restore tests exist, but only local/disposable environments are proven. Run the same drills in staging. |
| Technical distinctiveness | 7/10 | PostgreSQL as a durable queue with immutable versions is a defensible core; a generic “automation platform” pitch weakens it. Show a real developer event workflow. |
| User usefulness | 6/10 | The use case is useful to engineering teams, but live interoperability and a complete authoring journey are missing. Make one release/incident workflow excellent. |
| Security maturity | 8/10 in source; not production-reviewed | Strong tenant, secret, webhook, and egress controls; live integration setup and operator lifecycle remain. |
| Operational maturity | 5/10 | Runbooks, metrics, alerts, backups, rotation, and CI exist; no deployed staging, managed backup policy, or live alert/trace delivery. |
| Deployability | 4/10 | Local containerization is credible; no infrastructure, staging, cloud release, migration rollback, or live smoke test. |
| Evidence quality | 7/10 | Repeatable test/benchmark/restore evidence is stored and measured honestly. Add staging traces and provider failure evidence. |

A recruiter should remember: “A PostgreSQL-backed workflow engine that survives worker/database interruption and makes duplicate-delivery limits explicit.” A senior engineer should ask about transaction boundaries, leases, PostgreSQL queue trade-offs, idempotency, and external side-effect ambiguity.

## Part H — Keep / improve / refactor / replace / remove

| Subsystem | Decision | Reason |
|---|---|---|
| PostgreSQL state machine and queue | KEEP | Durable, tested core; no measured reason to add a broker. |
| Immutable workflow versions and run snapshots | KEEP | Gives replay/history stable semantics. |
| Worker leases, coordinator, recovery, DLQ | IMPROVE | Strong base; keep provider ambiguity visible and validate lease behavior in staging. |
| Workspace identity and role checks | IMPROVE | Preserve OIDC/RBAC model; stream revalidation and production TLS enforcement are implemented, but need staging verification. |
| Webhook inbox and event matching | KEEP | Signed, deduplicated, transactionally triggers runs. Complete metadata retention policy. |
| HTTP action | IMPROVE | Host/TLS/response bounds and DNS/action deadlines are implemented; validate provider idempotency and ambiguous outcomes. |
| Slack/GitHub integration | IMPROVE | Preserve the shared event path; validate against live provider accounts. |
| API tokens / SDK / CLI | KEEP, IMPROVE | Useful developer access path; role ceilings reduce delegated authority. Add per-action scopes only if real SDK users need them. |
| FastAPI modular monolith | KEEP | Suitable for current scale and simplifies transactions. Split route modules only to improve test/ownership boundaries. |
| Demo sandbox | KEEP, clearly label | Reproducible failure demonstration; never represent its charge action as payment. |
| Dashboard | IMPROVE | Good operator visibility and controls; add browser E2E and guided real workflow, not decorative complexity. |
| Redis/Kafka/Kubernetes/AI | REMOVE from near-term plan | No measured or product requirement supports the cost today. |

### Upgrade gap analysis

| Area | Current | Target | Gap | Priority | Action |
|---|---|---|---|---|---|
| Product | Durable developer-event workflows and operator history | One live, repeatable release/incident runbook | No live reference workflow or user validation | P0 | Verify one GitHub event to useful action in isolated staging. |
| Architecture | FastAPI modular monolith; PostgreSQL queue; separate worker role supported | Same code deployed as independently operated API/worker roles | No managed multi-process deployment evidence | P1 | Deploy the existing roles to staging; split services only if ownership/scaling requires it. |
| Python quality | Ruff, Mypy, pre-commit, typed SDK; 24 modules type-checked | Reproducible, reviewed release dependencies | No hosted release artifact; dependency ranges rather than a published lock/build provenance | P2 | Add a reproducible SDK release process if distribution is needed. |
| Backend | Authenticated workspace API, webhooks, SDK/CLI | Stable externally usable integration contract | Real external provider behavior is unverified | P0 | Run provider-backed smoke and failure contract checks. |
| Database | PostgreSQL stores definitions, queue, events, leases, and history; CI restore drill | Managed TLS database with tested lifecycle | No managed retention, migration rollback, or recovery evidence | P1 | Execute staging migration, backup/restore, and rollback/forward-fix drills. |
| Integrations | GitHub/Slack/OIDC code plus constrained HTTP; tests use mocks | One real provider-triggered workflow | No credentials/test workspace/staging configured | P0 | Configure isolated provider apps outside chat and run the real path. |
| Async/concurrency | Worker claims, leases, coordinator recovery, subprocess and restart coverage | Independently operated workers with measured behavior | No managed deployment or cross-host evidence | P1 | Validate unique worker IDs, drain/restart, and recovery in staging. |
| Distributed systems | At-least-once execution, dedupe, retries, DLQ/replay | Explicit recovery and provider outcome evidence | Ambiguous accepted side effects can repeat | P1 | Exercise provider duplicate/timeout cases; reconcile only when provider semantics permit. |
| AI/ML | None; not needed for workflow correctness | None unless a user problem justifies it | No gap | P3 | Keep AI/ML out of the core roadmap. |
| Testing | Broad PostgreSQL integration/failure tests, 80% coverage gate, CI restore and container smoke | Provider-backed contract, staging, full browser journey | No live provider/deployed or full OIDC browser proof | P0/P1 | Add opt-in provider checks and staging/browser smoke. |
| Security/data lifecycle | OIDC/RBAC, token ceilings, encryption, signed events, TLS enforcement, bounded egress | Same controls reviewed in deployment, with complete data lifecycle | No chosen event/backup/WAL retention policy, edge controls, or live review | P1 | Set retention and deploy with least privilege, ingress limits, and secret custody. |
| Observability | Request/run/task IDs, OTLP traces, Prometheus metrics/rules | Delivered alerts and useful deployed dashboards | Collector, Prometheus/Alertmanager, and alert delivery are not deployed | P1 | Verify trace export and one alert firing/recovery in staging. |
| Performance | Synthetic local workflow and query measurements | Reproducible staging baseline with resource conditions | No production-like workload/resource envelope | P2 | Capture throughput, queue wait, P50/P95/P99, DB contention, CPU, and memory before tuning. |
| Cloud | Container image and Compose, no cloud environment | Small single-region staging with managed PostgreSQL | No cloud account/IaC/environment selected | P1 | Choose a low-cost provider and provision only after a staging workflow is agreed. |
| Deployment/CI | GitHub Actions tests, security/dependency scans, restore and Compose smoke | Staging promotion and tested rollback/recovery | No deployment pipeline or managed release rollback evidence | P1 | Add a manually promoted staging release with health and migration gates. |
| UX | Dashboard for workspaces, integrations, run/event history | Complete configure → publish → run → inspect/recover journey | Authoring is API-heavy; full browser journey absent | P2 | Improve only around the proven workflow and automate its browser path. |
| Documentation | Architecture, deployment, security, status, limitations, evidence, ADRs | One current source of truth plus executed operator runbooks | Historical audit snapshots can disagree with current status | P2 | Keep this audit snapshot and `PROJECT_STATUS.md` current; retain older audits as history. |
| Portfolio evidence | CI, local synthetic benchmark, restore evidence, screenshots/demo | Live workflow, failure trace, deployment/recovery record | No live provider/staging artifact | P1 | Save redacted workflow/recovery evidence with exact versions and measured conditions. |

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

| Dimension | Current /10 | Target /10 | How to raise any score below 8 |
|---|---:|---:|---|
| Problem uniqueness | 5 | 7 | Own the narrow developer runbook use case and prove it with one live workflow. |
| Architecture depth | 8 | 9 | Show managed staging, worker recovery, and ambiguous external action behavior. |
| Technical creativity | 6 | 8 | Explain the transactional PostgreSQL/lease trade-offs with failure evidence, not technology count. |
| Real-world usefulness | 6 | 8 | Validate a real release/incident flow with an isolated engineering team. |
| Engineering difficulty | 8 | 9 | Add cross-host deployment and real provider failure/recovery evidence. |
| Interview potential | 8 | 9 | Publish the operating decisions and measured limitations. |
| Deployability | 4 | 8 | Deploy staging and prove migrations, restore, rollback, alerts, and secrets. |
| Demonstration strength | 8 | 9 | Pair the deterministic local demo with a short live provider run. |
| Measurable evidence | 7 | 8 | Repeat benchmarks with workload, hardware, DB size, and worker metadata. |
| Python Engineer relevance | 9 | 9 | Preserve depth and add real deployment operations; no keyword expansion needed. |

Scores are an evidence-based assessment, not measured product metrics. Target scores are goals, not claims.

## Part K — Target architecture

Keep the current modular monolith and PostgreSQL queue for the assumed small-team workload. Deploy API and worker roles separately in staging/production; the API should use RELAYCORE_WORKERS=0 when horizontally replicated. Keep the coordinator's lease/schedule/cleanup work behind safe PostgreSQL locking. Add a broker only after staging measurements demonstrate database queue contention or independent consumer scaling needs.

Target flow:

Developer provider or signed webhook → authenticated/verified intake → transactional event + matching immutable run → PostgreSQL ready queue → leased worker → bounded provider action → persisted result/event → dashboard, SDK, traces, and alerts.

Retain the shared database schema as the source of truth. Add provider receipts/reconciliation only for actions where the provider offers useful identifiers or idempotency guarantees. Do not pretend a local outbox can make an unrelated provider transaction atomic.

## Part L — Final tech stack

| Technology | Class | Why it exists; alternative considered | Operational complexity |
|---|---|---|---|
| Python 3.13 | ESSENTIAL | Existing backend, worker, SDK, tests, and libraries; replacing with Go/Node would rewrite working code without a measured benefit. | Runtime updates and dependency compatibility. |
| FastAPI + Pydantic | ESSENTIAL | Typed HTTP API and validation; Django would add an ORM/admin stack and require a rewrite, while current service needs a compact API. | ASGI lifecycle, framework upgrades, and request validation discipline. |
| PostgreSQL 18 + Psycopg 3 | ESSENTIAL | Transactions, row locks, uniqueness, leases, schedule state, and queue in one durable authority; Redis/SQS/Kafka add another consistency and operations boundary without load evidence. | Managed DB cost, connections, backups/WAL, vacuum, and migrations. |
| Process workers + coordinator | ESSENTIAL | Current durable execution path and crash/lease semantics; Celery or an external broker is unnecessary before a measured need. | Separate role lifecycle, unique worker IDs, shutdown, and lease tuning. |
| Authlib + OIDC | ESSENTIAL | Real user identity and verified issuer/session flow; static credentials remain demo-only. | Provider configuration, session lifecycle, callbacks, and live verification. |
| Fernet / cryptography | ESSENTIAL | Encrypt provider/webhook secrets stored in PostgreSQL; cloud KMS is a later custody option, not a substitute for key lifecycle design. | Master-key custody, backup compatibility, and rotation operations. |
| Uvicorn | USEFUL | Standard ASGI server for FastAPI; no custom server is warranted. | Process startup, health, and graceful shutdown configuration. |
| Docker + Compose | USEFUL | Reproducible local stack and CI split-role smoke; Compose alone is not production orchestration. | Image patching, secrets, storage, and deployment-specific config. |
| GitHub/Slack API clients | USEFUL | Real developer event sources/actions; additional connectors add little until this workflow is proven. | Scopes, provider changes, rate limits, and test installations. |
| OpenTelemetry + Prometheus format | USEFUL | Correlation and queue/lease metrics already implemented; a vendor agent could replace exporters but would couple operations to one vendor. | Collector/backend, alert routing, dashboards, and retention are not yet deployed. |
| Standard-library SDK/CLI | USEFUL | Simple authenticated API access without a client runtime dependency; package-index hosting is optional. | Compatibility/versioning and release maintenance if published. |
| Redis, Kafka, Celery, Kubernetes, AI/ML | REMOVE for now | No measured scale or product requirement needs them; they add cost, failure modes, and maintenance without closing current release gaps. | Additional infrastructure, security boundaries, and operational expertise. |

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
- Long-lived SSE polls revalidate workspace membership and session/token status; production PostgreSQL connections require `sslmode=verify-full`.
- Fernet-encrypted webhook and provider credentials; webhook HMAC over timestamp, event ID, and raw body; freshness, payload-size, and dedupe controls.
- HTTPS-only HTTP action with exact host allowlist, bounded public DNS resolution/address pinning, hostname TLS verification, no redirects, bounded response, and safe history; the worker caps provider action time to half its lease.
- Sandbox action separation and production rejection; request IDs and structured failure logs omit exception contents and secrets.

### Security priorities

1. Verify live OIDC/provider setup and authorization behavior in a controlled staging environment.
2. Define webhook event metadata, database backups, and WAL retention together.
3. Keep API tokens short-lived and retain current role ceilings; add finer scopes only if actual SDK use requires them.
4. Preserve the at-least-once external action contract and add provider receipts/reconciliation only where supported.
5. Add deployed ingress limits, secret-manager custody, alert/trace delivery, and an environment-specific security review before broad public ingress.

SSRF, XSS, CSRF, SQL injection, and prompt injection were reviewed at the source/design level. No AI or arbitrary-code execution path exists, so prompt-injection controls are not applicable.

## Part O — Reliability and failure behavior

| Failure | Current behavior | Remaining limit / expected target |
|---|---|---|
| Database unavailable | API/worker requests fail; worker reconnect loop and recovery tests cover interruption. | Stage with managed failover and test accepted work after recovery. |
| Worker exits or lease expires | Coordinator retries with bounded backoff or dead-letters; another worker may claim. HTTP/Slack actions renew before the provider call and use an action deadline capped at half the lease. | Verify provider delays and operational behavior in staging; timed-out DNS resolver threads cannot be cancelled, but concurrency is bounded. |
| Duplicate input | Unique event/idempotency constraints avoid duplicate runs for the same key/payload; mismatched payload conflicts. | Keep key-retention behavior aligned with metadata cleanup. |
| Queue full / rate limit | Admission returns a bounded error rather than growing an in-memory backlog. | Measure client retry/backoff behavior in staging. |
| HTTP provider timeout/5xx/429 | Retryable classification; stable idempotency key; bounded response; permanent failures dead-letter. DNS and HTTPS share one deadline; timeout does not open a connection. | Destination must honor idempotency; outcome can still be ambiguous if the provider accepted a request before a response was lost. |
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
- CI runs Ruff, Mypy, pre-commit, pytest with an 80% app-source coverage floor, both Compose config checks, browser/separate-worker smoke, Prometheus rule/config tests, a Docker image build and scan, a PostgreSQL 16 backup/restore job, and a Python dependency audit.
- There are real child-process and PostgreSQL restart tests, not only mocked unit tests.

### Gaps

- No full browser journey from OIDC login through workspace selection, token operations, live dashboard updates, and reconnect. CI has a narrower dashboard/separate-worker browser smoke.
- No live provider contract test or deployed smoke test.
- No managed production load/resource benchmark, property-based suite, or deployed alert/collector test.

Tests that matter next: real provider happy path plus duplicate/429/outage path; staging migrations/restore; deployed alert/trace delivery; and a complete browser journey once an OIDC test provider exists. Keep synthetic results labeled as simulated or local measured data.

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

### Phase 1 — Close authorization and action-lease gaps (implemented and locally/CI tested)

- **Objective:** ensure revoked users stop receiving stream data; prevent DNS and provider action deadlines from silently exceeding task lease assumptions.
- **Existing code:** app/main.py SSE route and identity dependencies; app/http_action.py DNS/HTTP path; worker lease heartbeat/recovery; tests/test_workflows.py and tests/test_http_action.py.
- **Keep:** workspace scoping, pinned HTTPS, immutable workflow state, at-least-once contract.
- **Change:** revalidate/terminate SSE streams on each poll; bound DNS resolution, renew the task lease before provider execution, and cap provider action time to half the lease.
- **Capability:** reliable revocation and bounded external request lifecycle.
- **Tests/security:** session, API token, and membership revocation during a stream; stalled resolver; shared DNS/HTTPS timeout budget; no outbound connection after timeout. CI also exercises the full PostgreSQL suite.
- **Observability:** log stream close reason and action timeout/lease outcome without payload or secret.
- **Acceptance/evidence:** no post-revocation events; timed-out resolver does not connect and action deadlines leave lease headroom. Regression coverage is present; the latest source CI run passed these checks.

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
- **Change:** verify managed-database TLS trust; add image digest release, migration compatibility/rollback process, secret manager configuration, external backups/WAL retention, health/readiness policy, and ingress limits.
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

1. **Passed in current code/CI:** no stream delivers events after session, token, or membership revocation.
2. **Passed for timeout budget in current code/CI:** a timed-out resolver does not open a network connection; provider action deadlines are capped at half the worker lease. Ambiguous accepted-provider outcomes remain at-least-once and are not claimed solved.
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
2. **Should any subsystem be rebuilt?** No full rebuild. SSE reauthorization, the bounded DNS/action deadline, TLS startup enforcement, and token role ceilings are implemented. Evolve provider reconciliation and deployment operations selectively.
3. **Is the project unique enough?** It is technically credible, but not a novel product category. Its defensible identity is a tested PostgreSQL-centered developer workflow engine with failure evidence.
4. **Five highest-value improvements?** (a) complete one live provider flow; (b) establish managed staging and verify migrations, restore, alerts, and traces; (c) define event/backup/WAL retention; (d) exercise duplicate, rate-limit, outage, and ambiguous-action behavior against a real test provider; (e) complete the browser user journey and measured staging benchmark.
5. **What should not be added?** AI/ML, Redis/Kafka, Kubernetes, multi-region, connector sprawl, or a broad visual builder before actual user demand and measurement.
6. **What would make this exceptional?** One live GitHub-to-action workflow, tested under duplicate delivery, worker loss, provider outage, restore, and staged deployment, with evidence another engineer can reproduce.
7. **What would make it look generic?** Calling it a no-code automation clone, claiming exactly-once/production scale, or presenting simulated charge/payment effects as real integrations.
8. **What should be built first?** The core security/timeout fixes are already present. The next phase is a narrow staging integration test once an isolated test provider and staging environment are configured.

## Current snapshot verification — 2026-10-09

- Local Ruff passed for `app`, `tests`, `benchmarks`, `scripts`, and `sdk`; Mypy passed for all 24 configured source files; pre-commit passed after putting the project Python 3.13 environment first on `PATH`.
- **25 database-independent tests passed** across `tests/test_http_action.py` and `tests/test_store_types.py`.
- The complete local PostgreSQL suite could not run: the configured disposable database at `127.0.0.1:55433` is unavailable and Docker is not installed on this machine. Compose validation and image/runtime smoke are therefore delegated to CI.
- Runtime code at `20a77d0` passed GitHub Actions run [37947393988](https://github.com/Jithendra-coder/RelayCore/actions/runs/37947393988): **131 tests, 82.98% app coverage**, browser/separate-worker smoke, Compose, Prometheus, SDK wheel, type/lint/pre-commit, and restore checks. Container security [37947393997](https://github.com/Jithendra-coder/RelayCore/actions/runs/37947393997) and dependency audit [37947393982](https://github.com/Jithendra-coder/RelayCore/actions/runs/37947393982) passed.
- Documentation-only revision `ce2b62c` passed GitHub Actions run [37947783510](https://github.com/Jithendra-coder/RelayCore/actions/runs/37947783510): **131 tests at 82.98% app coverage**, browser/separate-worker smoke, Compose, Prometheus, SDK wheel, type/lint/pre-commit, and PostgreSQL 16 restore of 17 migrations. Container security [37947783589](https://github.com/Jithendra-coder/RelayCore/actions/runs/37947783589) and dependency audit [37947783702](https://github.com/Jithendra-coder/RelayCore/actions/runs/37947783702) passed.
- No live OIDC, GitHub, or Slack credentials, `.env`, staging service, cloud environment, deployed Prometheus/Alertmanager/collector, or managed backup policy is configured here.

## Audit boundary

The inspection covered source/API/auth, integrations, persistence and migrations, worker/coordinator behavior, tests, SDK/CLI, deployment/configuration, CI, observability, benchmarks, and product documentation. It did not line-read every static HTML/CSS statement or every historical migration constraint. No runtime/application code was changed as part of this audit refresh.

## Historical remediation follow-up — 2026-10-08

This section records the changes that followed the original `0957c1f` audit snapshot; current findings and status are summarized above.

1. **SSE revocation:** each poll now validates current workspace membership and the backing session or API token. Regression tests revoke each credential/membership during an open stream and confirm it closes on the next poll.
2. **DNS and lease budget:** outbound HTTP and Slack actions use one total deadline across DNS, connection, and response handling. The worker renews the task lease immediately before the provider call and caps the action deadline at half the lease. DNS calls that cannot be cancelled are bounded by a four-slot semaphore; timeout/capacity failures do not open an outbound connection. Lease settings below 0.2 seconds are rejected.

3. **Container image scan:** the build removes pip after installing runtime dependencies, and CI asserts the resulting image has no pip package manager before running Trivy over OS and Python library packages. The assertion and HIGH/CRITICAL scan passed in [run 37794690468](https://github.com/Jithendra-coder/RelayCore/actions/runs/37794690468); no vulnerability suppressions were added.

The local PostgreSQL 17.9 suite passed **113 tests** at **81.84% app coverage**; Ruff, compileall, pre-commit, and `git diff --check` passed. PostgreSQL 18 CI passed **113 tests at 82.64% coverage**, plus Compose, Prometheus, image-build, and PostgreSQL 16 restore checks in [run 37788306422](https://github.com/Jithendra-coder/RelayCore/actions/runs/37788306422). The dependency audit passed in [run 37788306278](https://github.com/Jithendra-coder/RelayCore/actions/runs/37788306278). Live providers and production deployment remain unverified; external effects still have at-least-once semantics.

## Subsequent security and code-quality closure — 2026-10-08

4. **Database TLS:** the production API, standalone worker, and secret-rotation command now reject a connection string unless it explicitly sets `sslmode=verify-full`. A unit test checks acceptance of verified TLS, rejects weaker modes, and leaves Demo Mode's local PostgreSQL path unchanged. Managed database certificate trust still needs staging verification.
5. **Static type coverage:** Mypy now checks 24 app, SDK, operational-script, and benchmark modules with untyped function-body checks and `Any`-return warnings. The findings in the script/benchmark paths were resolved with typed Psycopg rows and explicit handling for empty query results and non-object smoke responses.

CI run [37803033807](https://github.com/Jithendra-coder/RelayCore/actions/runs/37803033807) passed all 24 Mypy modules, **116 tests at 82.72% app coverage**, the SDK wheel check, Compose validations and smoke, Prometheus checks, and PostgreSQL 16 restore drill. Container scanning and dependency audit passed in [37803033700](https://github.com/Jithendra-coder/RelayCore/actions/runs/37803033700) and [37803033706](https://github.com/Jithendra-coder/RelayCore/actions/runs/37803033706). Live provider tests, staging, and cloud operations are still open.

6. **API token authority:** new tokens default to `operator`; a signed-in workspace session can select `viewer` or explicitly choose `admin`. The current membership role caps the effective permission on each request. Existing tokens retain their old authority after migration 017, and a bearer token cannot mint another token. Run [37805748982](https://github.com/Jithendra-coder/RelayCore/actions/runs/37805748982) passed PostgreSQL 18 integration tests (**118 tests, 82.82% app coverage**), Compose validations, split-role smoke, Prometheus checks, and PostgreSQL 16 restore; container and dependency checks passed in [37805749071](https://github.com/Jithendra-coder/RelayCore/actions/runs/37805749071) and [37805749276](https://github.com/Jithendra-coder/RelayCore/actions/runs/37805749276).

The live OIDC/provider and staging limitations above still apply. Local PostgreSQL was unavailable during this follow-up; the complete database suite passed in CI.

