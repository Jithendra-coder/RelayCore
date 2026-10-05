# ADR 009: Constrained outbound HTTP actions

## Status

Accepted for the first production action adapter.

## Decision

- Production workflow definitions accept only the generic HTTP action and require a valid workspace credential. Production webhook-triggered starts remain disabled.
- Each HTTP bearer credential is bound to one exact DNS hostname. That hostname must also appear in the operator's `RELAYCORE_HTTP_ALLOWED_HOSTS` allowlist; wildcards are not supported.
- Require HTTPS on port 443, reject URL credentials and credential-like query/header values, resolve DNS before connecting, reject every non-global answer, and pin the selected public IP while TLS verifies the original hostname.
- Never follow redirects. Bound each step payload to 4 KiB, response bodies to 64 KiB, and socket/read timeouts to one second. Accept 2xx; retry 408, 429, and 5xx; send other non-2xx statuses to the dead-letter queue without retrying.
- Send `Idempotency-Key: <run-id>:<step-index>` to the provider. Store bounded JSON response metadata/body with credential-like JSON fields redacted.

## Consequences and limits

- Egress requires operator allowlist configuration, and every HTTP credential can reach only its one bound hostname.
- OS DNS resolution itself has no hard timeout in the standard library; resolver stalls can exceed a worker lease. The selected IP is pinned after resolution to prevent a second DNS lookup from rebinding the destination.
- Delivery remains at least once. Cancellation cannot stop a request already in flight; a completed response is recorded after cancellation, but duplicate external effects still depend on the remote service honoring the idempotency key.
- URL, body, and headers are static. Runtime input templating, response mapping between steps, OAuth flows, provider-native adapters, and live third-party endpoint tests remain future work.
