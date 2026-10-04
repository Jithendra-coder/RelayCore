# RelayCore architecture and invariants

## P0 state model

`workflow_runs`: `queued -> running -> completed | failed | cancelled`; `running` may pass through `retrying` after a lease expires. `tasks`: `queued -> running -> queued` for the next step, `retry_wait -> running`, or terminal `succeeded | dead | cancelled`. A task lease is valid only while its owner matches and `lease_until > clock_timestamp()`.

Invariants:

1. A workflow definition is bounded, validated data. Workers dispatch only registered actions; payload strings are never evaluated as code.
2. Workflow creation, its first task, its idempotency record, and its audit event commit in one PostgreSQL transaction.
3. Claim uses `FOR UPDATE SKIP LOCKED`; only one worker can own a valid lease at a time. Lease expiry is the recovery signal after a process disappears.
4. Each step's demo side effect, step advancement, and audit record commit together. A unique `(tenant_id, idempotency_key)` constraint prevents a repeated step from making a second logical effect in this database.
5. Each state change and administrative action writes an append-only event in the same transaction as that change.
6. Retry count is bounded. Backoff is deterministic and capped so Demo Mode is repeatable. Exhausted work enters the dead-letter table and the workflow becomes failed.
7. A tenant is derived from the authenticated API key; request data cannot choose or override it.
8. Queue admission checks a configured bound inside the enqueue transaction. The queue is durable, so accepted work survives API and worker process restarts.

## Delivery semantics and transaction boundaries

PostgreSQL is the durable database and queue. Workers poll it; a row lock assigns each ready task to one worker. Delivery is **at least once**: an expired lease can cause a task step to be attempted again. The application makes its database-backed logical effects idempotent by tenant and step key. This is not magical exactly-once delivery. A real external payment/email call cannot participate in this PostgreSQL transaction; use an idempotency key supported by that provider or a transactional outbox and reconcile ambiguous outcomes. A process dying before commit rolls back the step transaction; dying after commit leaves the advanced step durable, and a later lease owner continues from the next step.

Cancellation prevents new steps and asks an active worker to stop before committing its current step. A process killed by failure injection does not run cleanup: its lease remains until expiry, then bounded retry or DLQ applies. Queue saturation returns HTTP 429 rather than retaining an unbounded in-memory backlog. PostgreSQL stores leases and heartbeats as well as durable state; Redis is omitted because it would duplicate coordination state without solving a measured need here.

## Boundaries and flow

```text
Untrusted client
  | HTTPS in a real deployment; bearer API key, request-size/schema/rate limits
  v
FastAPI (one supervisor process; request ID + structured logs)
  | tenant-scoped transactions / role checks
  v
PostgreSQL (durable runs, tasks, leases, audit, dedupe, effects, DLQ)
  ^       | SELECT ... FOR UPDATE SKIP LOCKED
  |       v
Coordinator thread -> worker subprocesses (allow-listed actions only)
  |       | heartbeat / lease; crash -> expiry -> retry or DLQ
  +-------+ -> polled SSE / metrics / operations dashboard
```

The browser talks only to the API. API keys are supplied by environment configuration and never persisted by the server. DB credentials stay in environment variables. Dashboard data is tenant-scoped. Worker payloads are data; they cannot name a Python function or shell command. The default local Compose port binds to loopback. Production exposure requires TLS termination, secret rotation, backups, and managed database policy.

## Dependencies and deployment choices

FastAPI provides typed HTTP/OpenAPI validation. Psycopg provides PostgreSQL transactions and row locks. Uvicorn serves the API. PostgreSQL provides both durable state and work claiming, which is simpler than operating a second broker for this measured demo. Redis, Kafka, Celery, Kubernetes, cloud infrastructure, and third-party telemetry exporters are intentionally omitted until load or deployment evidence justifies their operating cost. Python 3.13 is used in the checked runtime and container because Python 3.12 was not installed in the build environment.

## Phase gates

| Phase | Evidence gate |
|---|---|
| P0 | State and invariants documented |
| P1 | Persistent create/run/history and restart integration check |
| P2 | Independent processes, heartbeat/lease expiry, bounded retry |
| P3 | Duplicate event and effect uniqueness checks |
| P4 | Competing workers, cancellation and bounded queue checks |
| P5 | DLQ inspection and authorized replay checks |
| P6 | API-key roles, tenant isolation, rate limits and audit checks |
| P7 | Correlated logs, dashboard, live event stream and metrics |
| P8 | Kill a real child worker and exercise deterministic recovery |
| P9 | Measured load run and PostgreSQL query-plan evidence |
| P10 | One-command local Demo Mode and reproducible evidence pack |

Cloud deployment and managed-service security are separate operational work: no cloud account, deployment credentials, or running Docker daemon were present during this build. Local Compose files will be supplied, and deployment status will be reported accurately.
