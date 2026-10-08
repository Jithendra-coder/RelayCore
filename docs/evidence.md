# RelayCore verification evidence

## Measured local load run

Report: [`benchmarks/results/local-run.json`](../benchmarks/results/local-run.json). The latest report was captured **2026-10-04 14:13 UTC** on Windows 11 with Python 3.13.7, PostgreSQL 18.3, and 12 logical CPUs. Each scenario submitted 100 synthetic two-step workflows with database-only `record` effects. No external payment provider or network latency was involved.

| Worker processes | Workflows/s | Steps/s | P50 latency | P95 latency | P99 latency | Failures | Peak active depth |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 27.00 | 54.01 | 2,798.00 ms | 3,096.99 ms | 3,109.73 ms | 0 / 100 | 100 |
| 2 | 33.89 | 67.78 | 2,319.08 ms | 2,364.86 ms | 2,366.68 ms | 0 / 100 | 100 |
| 4 | 37.23 | 74.46 | 2,186.75 ms | 2,289.27 ms | 2,296.70 ms | 0 / 100 | 100 |

This small local record-only load increased measured throughput by about 38% between one and four worker processes. It does not project cloud, multi-node, sustained, or provider-backed performance.

## PostgreSQL query plan

The same queue-ready `SELECT ... ORDER BY available_at, created_at LIMIT 1` ran against a temporary PostgreSQL table populated with 20,000 generated queue-shaped rows. `EXPLAIN (ANALYZE, BUFFERS)` measured 15.411 ms with a sequential scan and top-N sort before the partial ready-task index. After adding the index on `(available_at, created_at)` for `queued` and `retry_wait`, PostgreSQL used an index scan and measured 0.126 ms. The complete before/after plans and buffer counts are in the JSON report. This is a single local run on a temporary fixture; the latency delta is not a production claim.

## Correctness and failure checks

The acceptance suite completed with **72 passed** on an isolated PostgreSQL 17.9 / UTF-8 database on 2026-10-08; app source coverage was **80%**. The current phase also ran Ruff, pre-commit, Python compilation, and dashboard JavaScript syntax checks. GitHub Actions runs the same suite on PostgreSQL 18. It covers:

- API key authentication, role denial, request IDs, and cross-tenant `404` isolation.
- Idempotent workflow creation, payload conflict, persisted history, step transitions, and fail-once retry.
- Ten concurrent deliveries for one business key resolve to one workflow; the end-to-end duplicate path records 10 deliveries and three unique step effects.
- Tenant queue saturation returns HTTP 429; cancellation prevents the current step effect.
- A real child worker process is killed while it owns a lease; after lease expiry a different worker claims the step and completes the workflow.
- PostgreSQL terminates an active worker connection; the worker process reconnects and finishes after lease expiry.
- A separate Uvicorn process is killed and restarted; the queued workflow and original event history remain readable.
- A workflow resumes at its next step when the prior worker is lost after the step transaction commits. The effect and step advancement share one transaction, so there is no separate post-effect acknowledgement gap in this database-only action model.
- All idle API-pool connections are terminated; the pool health check discards stale connections and the next request reconnects successfully.
- PostgreSQL is stopped immediately and restarted while a real worker holds a lease; the API becomes healthy again, the expired lease is recovered, and the workflow completes. See [`database-restart.json`](database-restart.json).
- A bounded-failure demo reaches DLQ; admin replay releases the deterministic demo hold, completes the workflow, and retains the audit history.
- HTTP action validation rejects non-allowlisted/private destinations and host/credential mismatches before connecting; a no-network transport checks address pinning, verified TLS context setup, response caps/redaction, status retry classification, and the production workflow path.
- A signed production webhook starts one immutable HTTP workflow, duplicate delivery does not enqueue another, the run retains only safe source-event metadata, the worker receives the event for body mapping, and publishing version 2 does not alter an already queued version 1 run.
- Slack message action tests verify active workspace credential lookup, production workflow execution, bounded JSON requests, redirect rejection, and rate-limit retry delays. Live Slack delivery remains unverified; the at-least-once worker model can duplicate an accepted post after an ambiguous response.
- A production worker rejects an old queued simulated action and dead-letters it without writing an effect.
- A worker health check reports healthy only while a same-host database heartbeat is recent; runtime configuration rejects invalid worker, retry, queue, rate, and lease limits.

The suite emits one dependency deprecation warning from Starlette's current `TestClient` adapter for HTTPX. It does not affect the results. CI validates both Compose configurations and builds the image; the full Compose stack was not launched locally because Docker is unavailable in this environment.

## Live Demo Mode evidence

The clean local Demo Mode run is recorded in [`demo-run.json`](demo-run.json). It completed on 2026-10-04 against the isolated `relaycore_final` database with two independent worker processes. Ten deliveries of one business key yielded one workflow and three logical effects. A real child worker was killed while it held a lease; the lease expired, the other worker reclaimed it, and four unique step effects completed. A three-attempt failure entered the DLQ, was replayed by admin, and completed. Final queue depth and unreplayed DLQ size were both zero. The run's measured workflow P95 was 6.90 seconds; this includes the failure/retry delay and is separate from the synthetic load results above.

The dashboard capture [`relaycore-dashboard.png`](relaycore-dashboard.png) shows that completed recovery, the durable event timeline, duplicate counts, worker state, replayed DLQ entry, queue depth, and the same P95. The interactive local dashboard was also opened at `http://127.0.0.1:8013` for the review.

The PostgreSQL service was stopped immediately and restarted while a real worker held a lease. The API pool reconnected, the coordinator recorded lease expiry, worker-2 reclaimed the work, and the workflow completed with two logical step effects. Details are in [`database-restart.json`](database-restart.json). This proves recovery from one local database-process restart, not failover to another database node.

## Evidence boundaries

The “charge” is a unique local PostgreSQL side-effect record, not a payment. Recovery and process restart were measured in one local deployment. Public-cloud deployment, external-service idempotency, a full backup restore, TLS, and distributed tracing with an OpenTelemetry collector are not proven here. Docker Compose configuration and image build pass in CI, but the stack was not launched locally.
