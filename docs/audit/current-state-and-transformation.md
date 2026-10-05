# RelayCore current-state and transformation assessment

**Assessment date:** 2026-10-05
**Target role:** Python Engineer
**Audited revision:** `dc03e1f` (`main`, remote head confirmed)
**Scope note:** The audit began from committed revision `dc03e1f`. At the report's initial draft, Slack message actions were still uncommitted and one of 61 tests failed because an assertion JSON-serialized a Python `datetime` without a serializer. The follow-up implementation and verification status is recorded at the end of this document.

**Validation evidence for this assessment:** Ruff passed; Python compileall, dashboard JavaScript syntax check, Compose configuration validation, and `git diff --check` passed. PostgreSQL was unavailable at first, then the existing local cluster was recovered and the integration suite ran. OIDC/GitHub/Slack app credential variables were absent from the process environment, so no live provider check was possible. The current in-progress full suite result is recorded above; it is not green.

## A. Executive assessment

RelayCore is a Python/PostgreSQL durable workflow engine aimed at developer automation. Its credible center is database-backed execution and recovery: immutable workflow versions, transactional admission, worker leases, retry and dead-letter handling, event deduplication, and a testable restart path. It is more substantive than a CRUD demo, but it is not yet a production service. Identity-provider and provider integrations have no live credentials in this environment, the cloud stack is not deployed, external delivery remains at least once, and operational monitoring is incomplete.

**Current project identity, based on the code:** a locally verifiable, multi-tenant developer workflow engine with a production-oriented PostgreSQL core and mocked external integrations—not yet a live hosted automation product.

**Recommendation: Option A — incremental evolution.** Keep the PostgreSQL core and modular monolith. Improve the external-action boundary, split worker deployment before horizontal scaling, establish staging and live provider checks, and close the observability and operational gaps. A full rewrite or a second queue is not justified by the checked-in evidence.

The strongest product direction is a focused, self-hostable **developer event automation engine**: run versioned release and incident workflows from real repository events and signed webhooks, with durable progress, explicit retries, and recoverable failures. Avoid positioning it as a general Zapier replacement.

## B. What the committed code actually does

### Purpose and user journey

In local Demo Mode, a user opens the dashboard, submits a deterministic sample workflow, watches PostgreSQL-backed workers claim steps, kills a worker to exercise lease recovery, submits duplicate business events, and replays a dead letter. Demo actions record local effects; the “charge” action does not move money.

With production configuration, a person signs in through OIDC, creates or selects a workspace, and an owner/admin manages members, signed webhooks, encrypted credentials, or a GitHub/Slack app connection. A workflow author publishes an immutable definition version and starts it manually or binds it to an exact webhook event type. PostgreSQL admits the event and matching run transactionally. Workers execute bounded HTTP steps; the dashboard reads persisted history and SSE events. GitHub pull-request and Slack mention deliveries are normalized into this same event path.

The repository contains actual OIDC, HTTP, GitHub, and Slack protocol code, but the checked tests mock provider responses and HTTP transport. No live OIDC, GitHub, or Slack credentials are configured, so external interoperability is **unverified**.

### Architecture and boundaries

- FastAPI is a modular monolith for the dashboard, API, auth callbacks, integrations, SSE, and metrics.
- PostgreSQL is both the source of truth and the durable work queue. `SKIP LOCKED`, row constraints, transaction-scoped advisory locks, and unique idempotency keys coordinate admission and workers. Redis/Kafka are absent.
- The API lifespan starts a coordinator and worker subprocesses. This is reasonable for local use and one deployment, but duplicate worker identities make multiple API supervisors unsafe.
- Numbered migrations establish workflow history, OIDC/workspaces, webhook inbox, encrypted credentials, host-bound HTTP credentials, source events, GitHub App, and Slack App state.
- Production HTTP egress is limited to configured public HTTPS DNS hosts, checks and pins an address, verifies TLS against the hostname, rejects redirects, and bounds requests/results. Demo actions are isolated in `app/sandbox.py` and rejected by production workers.
- The browser consumes the API and SSE. No independent web gateway is needed at the present scale.

### Current production readiness

The code has meaningful production-oriented controls, but the deployable product is still a prototype. `compose.yaml` is a loopback local/demo setup; Docker Compose parses, but the full stack was not launched in this audit. There is a GitHub Actions workflow with PostgreSQL 18, Python 3.13, Ruff, pre-commit, and pytest. There is no staging environment, cloud deployment, infrastructure-as-code, migration rollback plan, alerting, or real provider smoke test. The top-level `ARCHITECTURE.md`, `docs/runbook.md`, and `TESTING.md` contain older descriptions that conflict with the current implementation/status; the latter still says 48 tests while `PROJECT_STATUS.md` records 59.

## C. Capability inventory

| Classification | Evidence from the committed revision |
|---|---|
| **REAL (local durable core)** | PostgreSQL-backed workflow/task state, transactions, immutable versions, worker leasing, recovery, retries, cancellation, DLQ/replay, tenant-level admission limits, and persisted history. Process/database restart paths have integration evidence. |
| **REAL (security mechanisms in code)** | OIDC claim verification, hashed sessions, workspace membership checks, same-origin mutation checks, Fernet credential storage, parameterized SQL, signed custom webhooks, timestamp/replay checks, secret redaction, and SSRF-aware HTTP transport. These still need deployment/provider review. |
| **PARTIAL** | Production OIDC, custom-webhook, GitHub App, Slack App, outbound HTTP, tenant observability, and the browser workflow are implemented but lack live third-party and cloud verification. API/worker lifecycle is coupled. |
| **MOCKED** | GitHub and Slack OAuth/provider exchanges, provider webhooks, and outbound HTTP transport in tests. They validate local parsing and control flow, not provider account configuration or network behavior. |
| **SIMULATED** | Benchmark workflow generation, deterministic failure injection, duplicate business-event traffic, and local queue-pressure evidence. These are repeatable engineering checks, not real customer workload. |
| **DEMO-ONLY** | `record`, `charge`, pause/failure actions and the sample inventory/payment/shipment narrative. They run only through the sandbox and do not perform provider business transactions. |
| **BROKEN** | No committed baseline defect was identified in this inspection. One initial Slack action test assertion failed on datetime serialization; it was fixed in the follow-up checkpoint below. |
| **PLANNED-ONLY / absent** | Durable scheduling, SDK, CLI, tracing/alerts, cloud deployment, live provider checks, real payment, provider response mapping, and broad workflow branches/approvals. |

## D. Strongest engineering and highest risks

### Worth preserving

- The transactional state model and immutable run snapshots are the project’s hard technical core.
- PostgreSQL is used for coordination rather than combining several stores without workload evidence.
- Concurrency, lease expiry, restart, duplicate event, tenant isolation, migration upgrade, and DLQ behavior have tests.
- Workflow input is bounded declarative data; workers dispatch a fixed allow-list and do not evaluate user code.
- Security boundaries are visible in the implementation: workspace-derived tenancy, encrypted secrets, host-bound egress, raw-body signatures, and secret-free history.
- The benchmark report labels results as local synthetic evidence instead of claiming production capacity.

### Ranked weaknesses

| Priority | Weakness | Why it matters / action |
|---|---|---|
| **P0 before production claims** | No live OIDC/provider or cloud verification; no staging, backups/restore evidence, migration rollout/rollback, or alerting. | Keep the product labeled as a prototype until a deployed environment passes an external smoke and recovery test. |
| **P1** | External side effect and database completion cannot commit atomically. HTTP can rely on provider idempotency only when honored; Slack does not provide the same guarantee for the proposed post action. | Define duplicate/ambiguous outcomes explicitly. Add reconciliation or an outbox only where it materially improves recoverability; do not promise exactly once. |
| **P1** | API lifecycle supervises workers; multiple replicas with duplicate worker IDs are unsafe. | Separate worker deployment and issue unique IDs before horizontal API scaling. |
| **P1** | Provider integrations are mock-tested only. | Add opt-in provider-backed sandbox/staging contract checks once credentials and isolated test workspaces are available. |
| **P1** | Payload retention is unbounded; encryption master-key rotation, dependency scanning, and third-party security review are absent. | Define event retention/key rotation and add dependency/security checks before handling real customer data. |
| **P2** | At audit start, docs disagreed on production actions and test count. | This finding is closed in the follow-up: reconciled `TESTING.md`, runbook, root architecture, README, deployment guide, and status against the implementation and 62-test run. |
| **P2** | No tracing, pool/lease/queue-lag dashboards, alert rules, type checking, or coverage threshold. | Add metrics and traces tied to request, run, task, event, worker, and attempt IDs; configure operational alerts. |
| **P2** | No schedule trigger, SDK, or CLI; the workflow authoring experience remains API-heavy. | Prioritize a small typed SDK/CLI only after the deployed core and real user journey are stable. |
| **P3** | No AI/ML feature. | Keep it that way unless an evidence-backed user problem emerges; LLMs add no value to durable dispatch correctness. |

## E. Capability discovery and product decision

The natural extension is not “more connectors.” It is a reliable developer automation path where a GitHub event or signed service event starts a version-pinned run, actions notify or call a narrow approved service, and failures remain inspectable and replayable. The strongest first users are small engineering/platform teams who need a self-hostable alternative for a few critical release/incident runbooks and value control over secrets, retries, and audit history.

GitHub and Slack are relevant because they produce developer events and support actionable workflows. Generic HTTP is useful for constrained handoffs. Email, payments, AWS services, model APIs, or extra connectors should be added only for a concrete end-to-end workflow. AI, RAG, agents, and ML are not relevant to the current reliability problem and should not be added for keyword coverage.

### Subsystem decisions

| Subsystem | Decision | Reason |
|---|---|---|
| PostgreSQL workflow/task state | **KEEP** | It is the demonstrated differentiator and is already transactionally designed. |
| API + worker monolith | **IMPROVE** | Retain shared code and DB boundaries; split deployment/lifecycle before replica scaling. |
| Demo sandbox | **KEEP, isolate** | Useful reproducible proof, but it is not the production architecture or business value. |
| OIDC/workspace security | **KEEP, verify live** | Sound local coverage; provider and deployment configuration remain untested. |
| Generic HTTP action | **IMPROVE** | Keep allowlist/DNS/TLS protections; add provider-backed contract/reconciliation evidence. |
| GitHub/Slack integrations | **IMPROVE selectively** | Preserve inbound durable event path; verify real installs and only add actions that complete a useful developer workflow. |
| Dashboard | **IMPROVE where it removes API friction** | Keep tenant-scoped live run/history view; UI polish alone does not address production gaps. |
| Redis/Kafka/microservices | **REMOVE from roadmap for now** | No measured requirement justifies another coordination plane or network boundary. |
| AI/ML layer | **REMOVE from proposed scope** | No model-dependent product need exists. |

## F. Final project vision

### What we should build

RelayCore should become a self-hostable, PostgreSQL-backed developer workflow runner for release and incident automation. Engineering teams configure a small number of trusted integrations, publish immutable workflows, and start them from real repository events or signed service events. Each run provides durable step state, bounded retries, operator-visible failure, and audited recovery.

### User, problem, difficulty, distinction

- **User:** a small software team, platform engineer, or release engineer that owns repeatable cross-system runbooks.
- **Why use it:** to avoid losing work or hiding partial failures when a worker, database connection, or provider fails mid-run.
- **Hard problem:** transactional admission plus at-least-once execution across independent workers and external systems, including duplicates, leases, cancellation, and ambiguous provider outcomes.
- **Distinct identity:** reliability and tenant/security boundaries are the product; the dashboard is the operator’s window into those guarantees.
- **Recruiter memory:** “A Python/PostgreSQL workflow engine with real recovery tests, immutable run versions, and honest external side-effect semantics.”
- **Senior-engineer discussion:** worker ownership, transaction boundaries, isolation, idempotency limits, leases, retries, cancellation, provider failure, and scale/deploy boundaries.

### Hard technical core

Keep the queue, workflow/task state machine, version pinning, and failure recovery as the non-UI proof. A screenshot cannot prove competing workers, transaction ordering, lease recovery, migration safety, or secret isolation; integration and restart evidence can.

## G. Generic-project and uniqueness audit

At present, a generic description could make RelayCore sound like “another Zapier clone.” Narrow the product to developer release/incident runbooks, show real GitHub/webhook-to-action paths, and lead with restart/concurrency evidence rather than connector count.

| Dimension | Current /10 | Target after evidence-backed roadmap /10 | How to raise scores below 8 |
|---|---:|---:|---|
| Problem uniqueness | 5 | 7 | Own developer runbooks and failure recovery instead of general-purpose automation. |
| Architecture depth | 8 | 9 | Prove external action ambiguity and separately deployed workers under staging failures. |
| Technical creativity | 6 | 8 | Show hard transactional and recovery decisions, not technology count. |
| Real-world usefulness | 6 | 8 | Complete one provider-backed release workflow with live users. |
| Engineering difficulty | 8 | 9 | Verify tenancy, concurrency, provider retries, and recoverability in a deployed environment. |
| Interview potential | 8 | 9 | Publish architecture tradeoffs, failure drills, and measured results. |
| Deployability | 4 | 8 | Add staging, secrets, backups/restore, migrations, rollback, and alerts. |
| Demonstration strength | 8 | 8 | Preserve deterministic local recovery demo and add a short real integration run. |
| Measurable evidence | 7 | 8 | Repeat benchmarks with workload/hardware metadata and show live queue/latency metrics. |
| Python Engineer relevance | 8 | 9 | Add deployed worker operation, telemetry, dependency/type checks, and real provider integration. |

These “target” scores are a goal, not a claim about current implementation.

## H. Upgrade gap analysis

| Area | Current | Target | Gap | Priority | Action |
|---|---|---|---|---|---|
| Product | Durable workflow prototype, broad intended scope | Narrow developer runbook product | No live reference workflow/user validation | P1 | Select one release/incident workflow and verify with a real isolated workspace. |
| Architecture | Modular FastAPI/PostgreSQL app; API supervises workers | Same modular code with separately deployable API/workers | Replica lifecycle/identity coupling | P1 | Split process entrypoint and unique worker IDs before horizontal scaling. |
| Python quality | Typed/Pydantic paths; Ruff; integration tests | Static types and reproducible dependency/security checks | No type check, lock/scan pipeline | P2 | Add type checking and dependency audit with low-maintenance tooling. |
| Backend | Auth, RBAC, APIs, events, SSE | Documented stable API and user-friendly authoring | Live production contract absent | P1 | Add staging smoke and versioned API examples. |
| Database | PostgreSQL is durable state + queue; 11 migrations | Backups/restore, rollout/compatibility, retention | No operational restore/rollback evidence | P0 | Prove backup restore and migration procedure in staging. |
| Integrations | HTTP action; GitHub/Slack inbound paths | One live end-to-end provider workflow each | Credentials and live contracts unavailable | P0 | Configure sandbox provider apps and add optional live checks. |
| Async/concurrency | Process workers, row locks, leases, recovery | Separate worker service and tested competing deployment | Supervisor coupling; no multi-host staging evidence | P1 | Separate lifecycle and test lease/recovery across replicas. |
| Distributed systems | At-least-once queue, dedupe, DLQ/replay | Clear external idempotency/reconciliation contract | External action/outbox semantics incomplete | P1 | Document and test ambiguous outcomes; add provider reconciliation only when possible. |
| AI/ML | None (not needed) | None unless validated need | None | P3 | Do not add AI/ML to the core. |
| Testing | Unit/API/PostgreSQL/concurrency/failure/benchmark; CI | Green repeatable CI plus provider/restore/E2E proof | No live provider, browser E2E, cloud test | P1 | Add optional provider contract checks and deployed smoke/recovery suite. |
| Security | OIDC/RBAC/encryption/HMAC/SSRF/CSRF controls | Live review, retention/key rotation, dependency scan | No third-party review or automated scanning | P1 | Add retention/key lifecycle, scan dependencies, review external trust paths. |
| Observability | Structured request logs, events, SSE, Prometheus metrics | Traces, queue/lease/provider dashboards, alerts | Missing traces, pool metrics, alert rules | P2 | Trace API→run→task→provider and alert on queue age/dead letters. |
| Performance | Synthetic worker/query measurements | Repeated baseline with p50/p95/p99 and resource data | No production-like or 8/16 worker runs | P2 | Define a staging workload and rerun measured benchmarks. |
| Cloud | Dockerfile and Compose | Minimal single-region staging/prod with managed Postgres | No cloud resources or IaC | P0 | Deploy a small staging environment with least privilege and backups. |
| Deployment/CI | GitHub Actions tests + local Compose | Staging promotion, migration and rollback drills | No CD/staging/rollback | P0 | Add a manually promoted staging release before automation. |
| UX | Dashboard for status, workspace, integrations, event history | Guided publish/run/replay workflow and clear failures | Authoring still API-centric; no browser E2E | P2 | Improve around the selected workflow, with keyboard/accessibility checks. |
| Documentation | Many architecture/security/status docs | One source of truth and operator-ready guide | Historical audit identified root architecture/runbook/test count drift | P1 | Reconcile docs against current endpoints and CI output. |
| Portfolio evidence | Benchmarks, evidence JSON, screenshot, demo | Reproducible live deployment and failure report | Evidence is local and partly historical | P1 | Archive test output, deployment version, config provenance, and recovery drill. |

## I. Target architecture

Keep one codebase and one PostgreSQL authority initially. Deploy two process roles from the same image: **API** handles OIDC, workspace configuration, webhook verification, workflow publication, and reads; **worker** claims and executes tasks with a unique identity. PostgreSQL owns definitions, event inbox, task state, leases, results, audit, dedupe, and DLQ. Inbound GitHub/Slack/custom events verify and normalize before one transaction persists the event and admits matching pinned runs. Workers dispatch only registered actions. The browser uses HTTP for commands and SSE for one-way progress; WebSockets are unnecessary.

**Trust boundaries:** browser/OIDC provider, webhook sender/provider, authenticated workspace, database, and external HTTP/Slack destination. Tenant ID always derives from verified workspace membership. Workflow payloads are bounded data, not executable code. Secrets remain encrypted in PostgreSQL and are unwrapped only in the action path. HTTP destinations remain operator allowlisted; Slack actions use the workspace’s linked installation.

**Failure boundaries:** API can restart without losing admitted work; workers can die and leases recover; duplicate events deduplicate by provider/event identity; DB outage fails requests and stops claims; provider timeout is retryable only under documented semantics; permanent provider rejection enters DLQ; cancellation prevents later actions and may not undo an in-flight action. No cache is authoritative. The database-backed queue is the current measured scaling boundary; revisit only from load evidence.

**Deployment target:** first create an isolated staging environment with managed PostgreSQL, TLS ingress, secret manager, separate API/worker processes, backups, migration/rollback drill, and a small alert set. AWS ECS/Fargate + RDS is a reasonable Python Engineer portfolio target, but only after a no-cloud-cost local/staging plan is agreed. Do not add Redis/SQS until measured PostgreSQL limits or queue isolation needs justify them.

## J. Technology decisions

| Technology | Class | Why it exists / alternative considered | Operational cost |
|---|---|---|---|
| Python 3.13 | **ESSENTIAL** | Primary language; strong web/backend and worker ecosystem. Keep one supported runtime. | Runtime patching and dependency compatibility. |
| FastAPI + Pydantic | **ESSENTIAL** | Typed API/input validation and OpenAPI; simpler than adding a separate API framework. | ASGI lifecycle/configuration and framework upgrades. |
| PostgreSQL + Psycopg | **ESSENTIAL** | Transactions, row locks, uniqueness, leases, and durable queue in one authority; Redis/SQS/Kafka would add operational state without current evidence. | Managed DB, connections, backups, vacuum, migration care. |
| Process workers + coordinator | **ESSENTIAL** | Real worker crash/lease behavior, lower complexity than an external broker for measured use. | Must split lifecycle and use unique IDs before replicas. |
| Authlib/OIDC | **ESSENTIAL** | Real identity, issuer/subject validation, workspace sessions; static keys are demo only. | Provider setup, key/session policy, live verification. |
| Cryptography/Fernet | **ESSENTIAL** | Protect DB-stored webhook and provider secrets. | Master-key custody/rotation and recovery. |
| Uvicorn | **USEFUL** | ASGI server compatible with FastAPI; a standard production server. | Process and shutdown configuration. |
| Docker/Compose | **USEFUL** | Reproducible local stack; production orchestration not yet present. | Image updates, secrets, persistent DB volume. |
| GitHub/Slack API code | **USEFUL** | Meaningful developer event sources/actions. | Scope, webhook configuration, API changes, rate limits, live tests. |
| Redis/Kafka/Celery | **REMOVE for now** | No measured workload or requirement demands a second queue/coordination store. | Would add operating cost, failure modes, and consistency boundaries. |
| LLM/RAG/agent/ML dependencies | **REMOVE from scope** | No AI/ML problem in the current product. | Would add cost/security/evaluation work without helping core guarantees. |

## K. Python Engineer alignment and uniqueness

The repository already demonstrates backend/API design, PostgreSQL, Python process concurrency, durable work, security boundaries, retries, and system design. Its strongest missing role evidence is operating a real deployment: worker separation, cloud/managed DB, real provider interoperability, observability, and dependency/type hygiene. Redis is listed as an example skill in the brief, but not required; adding it without need would weaken the architecture story.

There is no need for AI Engineer or Data Scientist alignment: no LLM inference, retrieval, model evaluation, statistical modeling, or data pipeline is justified by the product. Depth in reliable Python systems is the sharper story.

## L. User journey and failure behavior

**Target journey:** admin signs in → creates/selects workspace → links a least-privileged GitHub/Slack app or creates a signed webhook → publishes an immutable workflow → real event admits a run → worker executes bounded actions → UI shows correlated progress/result → operator inspects a failure and safely replays or resolves it.

| Failure | Required behavior |
|---|---|
| OIDC/provider unavailable | Sign-in fails closed with a safe message; no anonymous production fallback. |
| PostgreSQL unavailable | API health/readiness fails; no in-memory acceptance claim; workers reconnect and only reclaim after lease recovery. |
| Worker crash | Lease expires, bounded retry or DLQ; committed previous steps are not repeated. |
| Duplicate/stale event | Signature and timestamp check; unique provider event key prevents a second run. |
| External timeout after action accepted | Surface ambiguous/retrying state; HTTP requires provider idempotency support; Slack may duplicate; never claim exactly once. |
| Provider 429/outage | Honor provider retry hints where available, apply bounded retry/backoff, then DLQ visibly. |
| Concurrent publish/run/admission | DB constraints and row/advisory locks preserve idempotency and version snapshot. |
| Malformed/oversized payload | Reject before storage or action; omit submitted secrets/payload from error and logs. |
| Workspace permission changes | Re-check membership/role at command time; cross-workspace lookups fail closed. |
| Cache | None is authoritative; current system avoids stale-cache correctness bugs. |
| Deployment/migration failure | Do not receive traffic on incompatible schema; backup, migration check, health gate, rollback/forward-fix plan. |

## M. Security, tests, and observability

### Security priorities

Existing code includes OIDC claim verification and hashed sessions; role-checked workspace access; Fernet-encrypted secrets; parameterized SQL; strict workflow action models; HMAC timestamped webhook verification; rate limits; CSRF origin checks; escaped dashboard values; and host allowlisting, public DNS validation/address pinning, TLS, and bounded HTTP responses. There is no arbitrary code execution. These controls are locally testable but not a live security certification.

Before production, prioritize PII/event retention and deletion, encryption-key rotation and restore, dependency scanning/SBOM, a live external security review, edge ingress rate limits, and least-privilege cloud roles/network policy. AI prompt injection/tool permission controls are not applicable unless AI is later justified.

### Testing audit and target

Present: unit tests for auth/action logic, PostgreSQL API/integration tests, fresh and upgrade migrations, concurrency/idempotency, worker kill/recovery, database reconnect/restart, signed webhook tests, local synthetic benchmark, Ruff, pre-commit, and GitHub Actions CI. No type checker/coverage gate, browser E2E, live provider contract test, cloud smoke, backup restore, or network chaos suite exists.

Keep the current unit + PostgreSQL integration + concurrency/failure suite. Add only relevant test types: provider sandbox contract tests, one browser E2E for sign-in/configure/publish/run/history, staging backup/restore and deployment smoke, and bounded load/recovery tests. Property-based tests may be useful for JSON pointer/signature parsers if fuzz findings warrant them. ML/AI evaluation is irrelevant.

### Observability

Existing request IDs, structured logs, append-only events, run/task/worker IDs, SSE, Prometheus metrics, and dashboard aggregates provide a start. Target traces and metrics should carry `request_id`, `workspace_id` (hashed/controlled), `event_id`, `workflow_version_id`, `run_id`, `task_id`, `worker_id`, attempt, action kind, and provider status. Avoid payloads, message contents, tokens, and secrets in telemetry. Add alerts for DB readiness/pool pressure, oldest queued age, lease expirations, retries/DLQ rate, provider 429/5xx, and webhook rejection anomalies. No model/token/cost telemetry is needed.

## N. Performance, deployment, and cost

The declared workload is a small-team workflow product; current evidence is a local synthetic run only. Historical measurements in `BENCHMARKS.md`: 100 synthetic workflows had P95 3,096.99 ms (1 worker), 2,364.86 ms (2), 2,289.27 ms (4), with 0/100 failures; a 20k-row query was 15.411 ms before and 0.126 ms after an index in one run. Do not treat these as capacity promises.

Likely first bottlenecks are PostgreSQL connections/locks, the per-tenant event-order/admission locks, polling/lease churn, serialized writes, and slow external providers. Baseline staging throughput, P50/P95/P99, queue lag, action timeouts, DB query time, CPU/RAM, and retries before tuning. Re-run at representative worker counts and preserve raw conditions/results.

Today, no cloud cost is being incurred by the project itself; local Docker/PostgreSQL use developer resources. For a credible staging deployment, use one region, one small managed PostgreSQL, one API task, one worker task, a secret store, logs/metrics, and a controlled ingress. Avoid always-on extras until a real user requires them; stop staging outside validation windows. Price actual cloud resources before deployment. A multi-region DB, Redis, Kafka, Kubernetes, and model inference are not cost-justified.

## O. Transformation roadmap and acceptance gates

| Phase | Smallest vertical slice | Acceptance / evidence before next phase |
|---|---|---|
| **0 — Audit** | This report plus corrected single-source status/docs. | Report is reviewed; no production feature is treated as verified solely from mocks. |
| **1 — Stabilize Slack action slice** | Bounded static Slack message action with lease-safe timeout, validated API result, durable 429 retry delay, reconnect UI, tests, and docs. | **Complete locally:** Ruff, pre-commit, compile, JS syntax, PostgreSQL suite and Compose validation pass; token absent from run history; `git diff --check` clean. Live delivery remains unverified. |
| **2 — One live workflow** | Isolated test OIDC provider and GitHub/Slack app credentials; one inbound event to one useful action. | Sandbox workspace verification, duplicate delivery, permission denial, retry and uninstall/revoke tested with provider evidence. Credentials never enter repo. |
| **3 — Operational split and staging** | Run API and workers as distinct roles against managed PostgreSQL; health/readiness, secret manager, backups. | Unique worker IDs, deploy/rollback, migration, backup restore, process kill and queue recovery proven in staging. |
| **4 — Observability/security** | Provider/workflow traces, queue age/DLQ/lease metrics, alerts, retention and key-rotation runbooks, dependency scan. | Alerts fire in staging drills; telemetry contains correlation IDs and no secrets/payload content. |
| **5 — Product usability and evidence** | Guided workflow authoring/run/replay, compact SDK/CLI only if the API user journey demands it. | Browser E2E and one documented developer release scenario; benchmark rerun under stated conditions; portfolio evidence is reproducible. |
| **6 — Scale only from evidence** | Profile staging bottlenecks and revise DB indexes/worker pool/queue boundaries as measured. | Before/after evidence demonstrates the chosen change; only then consider a broker or partitioning. |

Scheduling, conditions, approvals, broad connector catalogs, multi-region hosting, and AI should not precede these gates.

## P. Portfolio and interview evidence

Show a short scenario: submit a signed GitHub/webhook event, observe the immutable version/run and worker, kill one worker, watch lease recovery, then inspect a deliberately dead-lettered failure and replay policy. Pair it with a diagram, a threat/failure model, reproducible CI, benchmark metadata, and an honest deployment-status page. A recruiter should be able to start local Demo Mode without mistaking it for a real payment or live provider.

Interview topics: why PostgreSQL is the queue; `SKIP LOCKED`/lease semantics; transaction boundaries; idempotency limits; at-least-once vs exactly-once; event ordering; cancellation races; webhook HMAC/replay; tenant isolation; SSRF-resistant DNS/TLS; migration strategy; worker separation; and why Redis/Kafka/AI were rejected.

## Q. Final verdict

1. **Preserve the foundation?** Yes. The tested PostgreSQL durable core is the strongest differentiator.
2. **Rebuild a subsystem?** Not wholesale. Separate API/worker deployment and strengthen provider-action reconciliation as targeted changes.
3. **Unique enough today?** Technically credible, product identity only moderately distinctive. Narrow it to developer release/incident runbooks and prove one live workflow.
4. **Five highest-value improvements:** live provider path; separate worker deployment; staging plus backup/rollback; correlated telemetry/security lifecycle; dependency and retention hygiene.
5. **Do not add:** AI/ML, Redis/Kafka, microservices, multi-region, or connector sprawl without a measured user need.
6. **Exceptional outcome:** live, secure end-to-end workflow in staging plus demonstrated crash/duplicate/provider recovery and repeatable operating evidence.
7. **Generic-project risk:** calling it a broad no-code automation clone, overemphasizing UI, or listing technologies without real failure evidence.
8. **Build next:** pursue one live, isolated developer workflow, then the operational deployment gates. Do not proceed to deployment claims before those gates.

## Follow-up implementation checkpoint

The project loop resumed after this audit. The Slack message-action slice is now implemented and locally verified: the PostgreSQL suite passes **64 tests** with **79% app source coverage**; Ruff, pre-commit, compileall, dashboard JavaScript syntax, Compose configuration, and whitespace checks pass. The UI lets a workspace admin reconnect an existing Slack installation after scopes change. Mocked tests cover bounded requests, permanent errors, 429 responses, durable `Retry-After` delay, and secret-free run history. An optional Compose override disables API-managed workers and runs a standalone worker container; an integration test proves it claims and completes a run. No live Slack, GitHub, or OIDC credentials are configured, so provider interoperability remains unverified. Staging deployment, backup/restore, telemetry, and SDK/CLI remain open roadmap work.
