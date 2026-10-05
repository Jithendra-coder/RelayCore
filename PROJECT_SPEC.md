# RelayCore project specification

RelayCore is intended to be a real-time developer automation platform built around PostgreSQL-backed durable workflows. It must use real identities, workspace authorization, external events and provider actions in production. Its sandbox is only a local demonstration path and must never stand in for production integrations.

## Product boundaries

- Durable PostgreSQL state is authoritative. Delivery is at least once; task retries and idempotent database effects provide effectively-once behavior only where the downstream action supports it.
- Workflow versions are immutable, and each run pins the exact version and definition hash it started with.
- Every protected workspace resource must resolve through the authenticated user's membership. Provider credentials and payload secrets must not enter logs, audit metadata, or browser responses.
- Do not execute user-supplied Python, shell, or other arbitrary code.
- Add infrastructure only when a measured requirement justifies its operational cost.

## Product areas

The target includes OIDC users and workspaces, durable workflow definitions and runs, GitHub and Slack integrations, secure generic HTTP actions, signed custom webhooks, durable scheduling, retries and replay, a real-time execution view, operational telemetry, deployment automation, a typed Python SDK, and an API-backed CLI.

## Current implementation boundary

The current build has a durable PostgreSQL queue/worker engine, immutable workflow versions, OIDC sessions and workspaces, tenant-scoped authorization, signed durable webhook intake, exact-match Demo Mode triggers, encrypted workspace credentials, constrained production HTTP and Slack message actions, a GitHub App installation flow with signed pull-request webhooks, and Slack OAuth with signed `app_mention` events. Demo action execution is isolated in `app/sandbox.py` and blocked by the production worker. Production workflows can be manually started or triggered by an exact webhook type and combine bounded HTTP steps with fixed Slack messages using the active workspace installation. Triggered runs pin the workflow version and event row; JSON Pointer references in HTTP bodies can read from that event. URLs/headers/message text and HTTP response-to-next-step mapping remain static. Both integrations and Slack message posting are locally tested with mocked API responses but have no live credentials; GitHub API actions, scheduling, cloud deployment, SDK, and CLI remain incomplete. External delivery is at least once; ambiguous Slack post responses may produce duplicates. See [docs/audit/existing-system.md](docs/audit/existing-system.md), [PROJECT_STATUS.md](PROJECT_STATUS.md), and [LIMITATIONS.md](LIMITATIONS.md) for evidence and limits.
