# RelayCore architecture

## Current system

FastAPI serves the dashboard, API, and server-sent events. PostgreSQL stores tenant-scoped runs, task state, leases, retry times, workflow definitions and immutable versions, event history, idempotency records, effects, dead letters, users, workspaces, memberships, and hashed browser sessions. Independent worker processes claim tasks with row locks and `SKIP LOCKED`; a coordinator recovers expired leases. PostgreSQL is both the durable queue and source of truth. Redis and Kafka are not used.

An OIDC callback validates the provider claims through Authlib and creates a revocable server-side session. Each protected request resolves the selected workspace from the current membership before using its ID as the tenant key. Demo API keys use a separate, explicit Demo Mode path.

Workspace administrators can create HMAC-signed webhook endpoints. Their random keys are Fernet-encrypted at rest; requests are body-limited, timestamp-checked, and deduplicated by endpoint/event ID. The inbox row and secret-free SSE metadata commit in one PostgreSQL transaction. Demo Mode can match a JSON `type` to an immutable workflow trigger and queue the pinned version atomically; production workflow creation is still disabled.

## Guarantees and limits

Delivery is at least once. Database effects are idempotent by tenant and step key. The database cannot atomically commit an external provider side effect; a future adapter must use provider idempotency or an outbox and reconciliation. Current production workflow submission is intentionally unavailable because only sandbox actions exist.

The API process starts its own worker supervisor. Do not run multiple API supervisors with duplicate worker IDs against the same database. Horizontal production scaling needs a distinct worker service and unique worker identities.

See [docs/architecture.md](docs/architecture.md), [docs/adr/004-multitenancy.md](docs/adr/004-multitenancy.md), and [PROJECT_STATUS.md](PROJECT_STATUS.md) for detailed invariants and evidence.
