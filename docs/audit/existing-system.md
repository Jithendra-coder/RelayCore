# Current system audit

Audit snapshot: 2026-10-08, based on the current `main` worktree. This file is the current source-based overview; see `PROJECT_STATUS.md` for the latest test and delivery evidence.

## Purpose and architecture

RelayCore is a self-hostable developer event workflow engine. FastAPI serves the API and dashboard; PostgreSQL stores workflow definitions, immutable versions, incoming events, task state, leases, effects, and dead letters. Independent Python workers claim work from PostgreSQL and execute only registered actions.

Custom signed webhooks, GitHub pull-request deliveries, and Slack app mentions can match exact event types and admit version-pinned production runs. Production steps are constrained to host-bound HTTPS requests and fixed Slack messages. Demo actions such as `charge`, delays, and failure injection run only in the local sandbox.

## Strong foundations to preserve

- PostgreSQL transactions, constraints, `SKIP LOCKED`, and expiring leases provide durable admission and recovery without a second queue.
- Immutable workflow versions keep a published run stable when a later version is published.
- Workspace membership derives tenant access; negative tests cover cross-workspace access.
- Webhook signatures, delivery deduplication, encrypted credentials, host restrictions, bounded responses, and secret redaction are tested locally.
- Worker failure, database restart, API restart, migration upgrade, replay, and concurrency have PostgreSQL integration coverage.

## Capability boundaries

| Classification | Current evidence |
|---|---|
| **Implemented and locally tested** | Durable workflow execution, retries, lease recovery, cancellation, dead-letter replay, immutable versions, interval schedules, OIDC/session and workspace code, signed webhook intake, HTTP action controls, and event-triggered runs. |
| **Implemented, provider unverified** | OIDC sign-in, GitHub App installation/webhooks, Slack OAuth/events/message posting, and outbound HTTP. Tests use mocked provider responses or a no-network transport; no live provider credentials are configured. |
| **Simulated** | Synthetic benchmark traffic and the Demo Mode inventory/payment/shipping story. The `charge` action records a local database effect, not a payment. |
| **Incomplete** | SDK/CLI, cloud deployment, OpenTelemetry traces and deployed alerts, cleanup of retained event metadata, encryption-key rotation, managed backup retention/disaster recovery, and live provider verification. |

## Priorities

- **Before public use:** define metadata retention, configure managed backups/disaster recovery, and add edge request limits and key lifecycle controls. Raw payload expiry is implemented with active-run and DLQ-replay safeguards, and a disposable CI restore drill now passes.
- **Before staging:** verify worker health and Compose production settings, exercise a real isolated provider workflow, and deploy API and worker as distinct roles.
- **After the core is proven:** add traces/alerts, type and dependency checks, and a small SDK/CLI if the real user flow benefits from them. Scheduling currently covers durable intervals, not cron/timezone calendars.

## Architecture decision

Preserve and evolve the PostgreSQL-backed modular monolith. Do not add Redis, Kafka, Kubernetes, AI/ML, or a connector catalog until measured workload or a validated user need requires them. The main reliability limit is at-least-once delivery: an external action can repeat after an ambiguous response, so the destination must honor its idempotency key or support reconciliation.
