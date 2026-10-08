# ADR 006: Signed custom webhook ingress

## Status

Accepted for the first durable inbox implementation.

## Decision

- Expose a workspace-managed opaque endpoint ID. Inbound requests are authenticated with an endpoint-specific HMAC secret; user browser sessions are never used for third-party callbacks.
- Require a JSON object of at most 256 KiB, an event ID, and a Unix timestamp. The HMAC covers `timestamp + newline + event ID + newline + exact raw body`.
- Reject timestamps outside a five-minute window. Persist the raw body and parsed JSON in PostgreSQL in the same transaction as a workspace-scoped event record.
- Use `(endpoint_id, event_id)` as the idempotency key. Identical replays acknowledge the original event; a changed payload under the same key is a conflict.
- Use PostgreSQL as the authoritative inbox and emit only secret-free metadata through the existing durable event stream.

## Consequences and limits

- The local per-workspace fixed-window limiter counts requests after endpoint lookup, including invalid signatures. Production ingress should also have edge-level IP/network controls.
- Raw bytes and parsed JSON expire after `RELAYCORE_WEBHOOK_PAYLOAD_RETENTION_DAYS` (30 days by default), except while a nonterminal workflow run needs the source event. Event IDs, hashes, types, and dedupe keys remain for history and duplicate detection; their retention is still unbounded.
- Demo Mode matches an exact JSON `type`, endpoint ID, and immutable current workflow version. Production workflow creation remains disabled, so production webhook events are durably stored but do not queue runs.
- See [ADR 005](005-secrets-management.md) for secret encryption and master-key limits.
