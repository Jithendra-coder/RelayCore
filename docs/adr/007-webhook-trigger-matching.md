# ADR 007: Exact-match webhook workflow triggers

## Status

Accepted for the sandbox trigger foundation.

## Decision

- Store a webhook trigger inside the immutable workflow-version definition. Its only condition is an exact match on workspace-owned endpoint ID and JSON `type`.
- At intake, select active workflows' current immutable versions, then create their runs in the same transaction as the new inbox row. Run idempotency keys derive from endpoint, event ID, and workflow-version ID.
- A duplicate event does not re-evaluate triggers. Publishing a later version cannot alter a run already queued from an earlier version.
- Execute trigger matching only in Demo Mode while the action registry contains sandbox actions. Production intake remains durable and observable but does not start runs until real production actions are available.

## Consequences and limits

- Trigger matching is deterministic but intentionally limited to a string equality check. JSONPath, compound conditions, branches, and schedules are not implemented.
- Queue saturation rejects the webhook transaction with 429; the sender must retry. This avoids acknowledging an event that did not enqueue all matching runs.
- This foundation is not a claim that production event-driven automation is complete.
