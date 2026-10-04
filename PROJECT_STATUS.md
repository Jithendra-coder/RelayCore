# RelayCore project status

## Current phase

P10 — local Demo Mode and evidence pack complete. The dashboard and API were run against an isolated PostgreSQL 18 database on loopback. Docker Compose parses but could not be launched because Docker Desktop's daemon was unavailable. No public-cloud account or deployment credentials were available, so this is not a public deployment.

## Phase gates

| Phase | Status | Evidence |
|---|---|---|
| P0 State model | Complete | State machines, lease rules, invariants, transaction boundaries, trust boundaries and dependency choices in `docs/architecture.md`. |
| P1 Durable workflow | Complete | PostgreSQL persistence, history and API process restart check in `tests/test_service_restart.py`. |
| P2 Worker leases | Complete | Independent worker processes, heartbeats, lease expiry, capped retry and recovery coordinator. |
| P3 Duplicate safety | Complete | Concurrent duplicate delivery test, 10-to-1 demo, unique effect constraint and post-commit worker-loss test. |
| P4 Concurrency and pressure | Complete | `SKIP LOCKED` claims, cancellation, per-tenant queue bound and concurrency tests. |
| P5 DLQ/replay | Complete | Bounded failure, admin replay and audit history tests plus recorded live demo. |
| P6 Security controls | Complete | API-key roles, tenant-scoped reads, write authorization, rate/quota admission and administrative audit events. |
| P7 Observability | Complete locally | Structured request logs, request IDs carried through durable task history, SSE, Prometheus text metrics and the dashboard expose queue, worker, retry, DLQ and latency state. OpenTelemetry span export and database-pool metrics are not implemented. |
| P8 Failure engineering | Complete locally | Real worker kill, terminated worker connection, API process restart and immediate PostgreSQL stop/restart recovery evidence. This does not model multi-node failover or network partitions. |
| P9 Measurement | Complete locally | Worker-count load run and before/after PostgreSQL query plans in `benchmarks/results/local-run.json`. |
| P10 Demo/evidence | Complete locally | One repeatable Demo Mode evidence run, database restart report and dashboard screenshot under `docs/evidence/`. Compose runtime and public hosting remain unavailable here. |

## Latest validation

- Latest full suite: **13 passed, 1 warning** on PostgreSQL 18.3 / UTF-8.
- Warning: installed Starlette deprecates its `httpx` TestClient adapter and points to `httpx2`.
- `python -m compileall -q app tests benchmarks scripts` passed after the final code and test changes.
- Both PowerShell scripts parse cleanly; `docker compose config --quiet` passed. The Docker daemon was unavailable, so no container image or Compose runtime was tested.
- Local live Demo Mode completed: 10 duplicate deliveries -> 1 workflow -> 3 effects; killed worker -> lease expiry -> replacement worker -> 4 unique effects; DLQ replay completed; final queue depth 0 and unreplayed DLQ 0. Its measured workflow P95 was 6.90 s, which includes recovery delay.
- Synthetic 100-workflow benchmark P95: 3,096.99 ms (1 worker), 2,364.86 ms (2), 2,289.27 ms (4); failures 0/100 in each scenario. The disclosed local environment and limitations are in `docs/evidence.md`.
- Temporary 20,000-row queue query plan: 15.411 ms sequential scan/sort before the partial index, 0.126 ms index scan after, one local run.

## Known limits, bugs and technical debt

- No known failing tests in the latest full suite.
- Workflow “charge” is a local PostgreSQL effect. A real provider call needs provider-side idempotency or an outbox/reconciliation path.
- This release uses PostgreSQL as the durable queue instead of Kafka/Redis because no separate broker was needed for the measured two-worker demo. That limits independent broker scaling and broker-specific replay behavior.
- Distributed OpenTelemetry traces/export, DB pool metrics, and cloud/TLS/backup deployment are not implemented or verified.
- Worker IDs are scoped to one API supervisor. Do not run multiple API supervisors with identical worker IDs against the same database; deploy a distinct worker service with unique IDs before horizontal API scaling. The demo failure was caused by two local supervisors sharing these IDs and was resolved by stopping the extra verification server.
- Compose configuration is validated but its runtime is unverified because Docker Desktop was stopped. No public cloud deployment was attempted without credentials or an authorized destination.

## Next smallest milestone

The remaining external milestone is to verify the Compose runtime with Docker available. A public rollout also needs a managed target, TLS, secret rotation, and backups. OpenTelemetry span export is a separate observability enhancement.
