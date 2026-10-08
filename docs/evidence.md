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

The latest application acceptance suite completed with **113 passed** on an isolated PostgreSQL 17.9 / UTF-8 database on 2026-10-08; app source coverage was **81.84%** against an enforced 80% floor. Ruff, Python compilation, pre-commit, and `git diff --check` passed for that source slice. Commit `4ee7049` passed PostgreSQL 18 CI with **113 tests at 82.64% app coverage**, the PostgreSQL 16 backup/restore drill, both Compose checks, the split API/worker Compose smoke, and Prometheus validation in [run 37794690373](https://github.com/Jithendra-coder/RelayCore/actions/runs/37794690373). Its image security and Python dependency jobs also passed in [runs 37794690468](https://github.com/Jithendra-coder/RelayCore/actions/runs/37794690468) and [37794690517](https://github.com/Jithendra-coder/RelayCore/actions/runs/37794690517). It covers:

Commit `fc51b97` adds Mypy to development dependencies and CI. Run [37797132307](https://github.com/Jithendra-coder/RelayCore/actions/runs/37797132307) passed Mypy for all 18 application and SDK modules, **114 tests at 83% app coverage**, both Compose validations, split-role Compose smoke, Prometheus checks, and the PostgreSQL 16 restore drill. The image scan and dependency audit passed in [37797132352](https://github.com/Jithendra-coder/RelayCore/actions/runs/37797132352) and [37797132249](https://github.com/Jithendra-coder/RelayCore/actions/runs/37797132249).

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
- Webhook payload cleanup clears raw bytes and parsed JSON after expiry, retains hash/type/dedupe metadata, protects nonterminal runs, skips active events so they cannot stall later cleanup, and rejects event-dependent DLQ replay without changing its dead-letter or task state.
- Slack message action tests verify active workspace credential lookup, production workflow execution, bounded JSON requests, redirect rejection, and rate-limit retry delays. Live Slack delivery remains unverified; the at-least-once worker model can duplicate an accepted post after an ambiguous response.
- SSE integration tests revoke the active cookie session, API token, or workspace membership while a stream is open and verify the next poll closes the stream. HTTP and Slack actions share one DNS-plus-request deadline capped at half the worker lease; tests verify DNS consumes that budget and timed-out HTTP DNS opens no provider connection.
- A production worker rejects an old queued simulated action and dead-letters it without writing an effect.
- A worker health check reports healthy only while a same-host database heartbeat is recent; runtime configuration rejects invalid worker, retry, queue, rate, and lease limits.
- Durable schedules are workspace-scoped and idempotent; the API coordinator dispatches a due row, two concurrent passes dispatch it only once, queue saturation leaves it due while another tenant proceeds, each run pins the then-current immutable version, and event-dependent versions pause safely.
- The restore-drill harness refuses to run without explicit opt-in and confines database creation to distinct localhost names prefixed `relaycore_restore_`.

The suite emits one dependency deprecation warning from Starlette's current `TestClient` adapter for HTTPX. It does not affect the results. GitHub Actions run [37770598389](https://github.com/Jithendra-coder/RelayCore/actions/runs/37770598389) passed Compose validation and image build for the webhook-retention commit. The full Compose stack was not launched locally because Docker is unavailable in this environment.

## PostgreSQL backup and restore drill

GitHub Actions run [37772675726](https://github.com/Jithendra-coder/RelayCore/actions/runs/37772675726) passed the `backup-restore` job on 2026-10-08. Against disposable PostgreSQL 16.15 source and target databases, the job ran the repository's PowerShell backup and restore scripts, validated the custom-format archive, and confirmed that all **13 migrations** and a known queued workflow record survived the restore. The archive and databases were ephemeral CI resources. This is repeatable script-level restore evidence; it does not establish managed backup retention, cloud recovery, or disaster recovery.

The resolved runtime and development requirements were scanned with `pip-audit` on 2026-10-08; it reported no known vulnerabilities in [GitHub Actions run 37794690517](https://github.com/Jithendra-coder/RelayCore/actions/runs/37794690517). The workflow runs on pushes, pull requests, manual dispatch, and weekly. This check does not scan the container's OS packages or detect malicious packages.

## Runtime image security scan

The final image installs runtime requirements, then removes pip; CI asserts `importlib.util.find_spec("pip")` is absent in the built image. This removes the image's bundled pip package-manager inventory from runtime while keeping pip available during the build. Trivy scans OS and Python library packages in that same image for HIGH and CRITICAL advisories; the build, assertion, and scan passed in [run 37794690468](https://github.com/Jithendra-coder/RelayCore/actions/runs/37794690468). Earlier scans reported outdated package versions without package paths even though the image's installed Python distributions contained only `urllib3 2.8.0`; Trivy warns that third-party SBOMs may be interpreted inaccurately. No vulnerability exceptions were added. The container scan complements, but does not replace, `pip-audit` or live security review.

The queue/lease metric integration test creates an hour-old runnable task and an expired lease in a rolled-back PostgreSQL transaction. It checks tenant scoping and unauthenticated denial. Workspace scrape tokens are hashed, read-only, scoped to one workspace, and revocable; API tests prove they can read `/metrics` and cannot call `/api/status`. Prometheus `promtool` checks the config syntax and all three alert rules, then runs rule unit tests that assert alerts fire after sustained queue age, expired leases, and dead letters while remaining quiet at the threshold boundary. An OTLP exporter test sends a span over HTTP to a loopback receiver, decodes the OTLP protobuf, and checks its service name and span. Optional OpenTelemetry spans exclude workflow payloads and exception messages; W3C trace context persists through durable tasks. GitHub Actions run [37778241792](https://github.com/Jithendra-coder/RelayCore/actions/runs/37778241792) passed **88 tests** on PostgreSQL 18 with **81.57% app source coverage** and an enforced **80% floor**, both Compose validations, Prometheus rule checks/tests, and image build. Its PostgreSQL 16 restore drill restored all **15 migrations** and a known workflow row. Dependency audit run [37778241641](https://github.com/Jithendra-coder/RelayCore/actions/runs/37778241641) passed. These checks do not prove a deployed scrape, notification delivery, or collector/backend processing; no Prometheus, Alertmanager, or OpenTelemetry collector has been deployed.

## Database encryption-key rotation

The offline re-encryption command was exercised against an isolated schema on local PostgreSQL 17.9 as part of the **106-test**, **81.51% coverage** full run. Tests verified webhook secrets, all integration credential versions, and GitHub PKCE verifiers decrypt under the new key; credential creation and rotation idempotency still work after fingerprint rekeying; and a corrupt ciphertext rolls back earlier table updates. PostgreSQL 18 CI passed in run [37780848171](https://github.com/Jithendra-coder/RelayCore/actions/runs/37780848171). The test does not verify production secret-manager updates, a maintenance rollout, or restoring pre-rotation backups with the escrowed old key.

## Workspace API tokens and Python client

The latest local PostgreSQL run covers one-time token display, SHA-256-only database storage, expiry/revocation, workspace binding, membership removal, live role changes, permission denial for workspace creation, and write-rate limiting. Token secrets do not appear in list responses. The standard-library SDK rejects non-loopback HTTP and redirects, preserves stable idempotency keys, and returns typed models. CLI tests cover environment-based credentials and workflow JSON input. `pip install --no-deps -e .` succeeded locally and the installed `relaycore --help` command ran. PostgreSQL 18 CI for this API/SDK/CLI change is pending; there is no package-index release or live OIDC sign-in configured.

## Live Demo Mode evidence

The GitHub Actions Compose smoke built and launched an isolated PostgreSQL 18/API/standalone-worker stack. It submitted the four-step Demo Mode workflow through the API, verified the worker completed it with four persisted step results, and removed the temporary Compose volume. This verifies container startup and local worker coordination, not live provider delivery or managed staging. See [run 37790505388](https://github.com/Jithendra-coder/RelayCore/actions/runs/37790505388).

The clean local Demo Mode run is recorded in [`demo-run.json`](demo-run.json). It completed on 2026-10-04 against the isolated `relaycore_final` database with two independent worker processes. Ten deliveries of one business key yielded one workflow and three logical effects. A real child worker was killed while it held a lease; the lease expired, the other worker reclaimed it, and four unique step effects completed. A three-attempt failure entered the DLQ, was replayed by admin, and completed. Final queue depth and unreplayed DLQ size were both zero. The run's measured workflow P95 was 6.90 seconds; this includes the failure/retry delay and is separate from the synthetic load results above.

The dashboard capture [`relaycore-dashboard.png`](relaycore-dashboard.png) shows that completed recovery, the durable event timeline, duplicate counts, worker state, replayed DLQ entry, queue depth, and the same P95. The interactive local dashboard was also opened at `http://127.0.0.1:8013` for the review.

The PostgreSQL service was stopped immediately and restarted while a real worker held a lease. The API pool reconnected, the coordinator recorded lease expiry, worker-2 reclaimed the work, and the workflow completed with two logical step effects. Details are in [`database-restart.json`](database-restart.json). This proves recovery from one local database-process restart, not failover to another database node.

## Evidence boundaries

The “charge” is a unique local PostgreSQL side-effect record, not a payment. Recovery and process restart were measured in one local deployment. Public-cloud deployment, external-service idempotency, TLS, and distributed tracing with an OpenTelemetry collector are not proven here. The CI restore drill passed against disposable PostgreSQL 16 databases; managed retention and disaster recovery remain unverified. Docker Compose configuration and image build pass in CI, but the stack was not launched locally.
