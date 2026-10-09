# ADR 009: Constrained outbound HTTP actions

## Status

Accepted for the first production action adapter.

## Decision

- Production workflow definitions accept only the generic HTTP action and require a valid workspace credential. Production webhook-triggered starts remain disabled.
- Each HTTP bearer credential is bound to one exact DNS hostname. That hostname must also appear in the operator's `RELAYCORE_HTTP_ALLOWED_HOSTS` allowlist; wildcards are not supported.
- Require HTTPS on port 443, reject URL credentials and credential-like query/header values, resolve DNS before connecting, reject every non-global answer, and pin the selected public IP while TLS verifies the original hostname.
- Never follow redirects. Bound each step payload to 4 KiB, response bodies to 64 KiB, and the total outbound action deadline to one second before the worker-lease cap. Accept 2xx; retry 408, 429, and 5xx; send other non-2xx statuses to the dead-letter queue without retrying.
- Send `Idempotency-Key: <run-id>:<step-index>` to the provider. Store bounded JSON response metadata/body with credential-like JSON fields redacted.

## Consequences and limits

- Egress requires operator allowlist configuration, and every HTTP credential can reach only its one bound hostname.
- The standard-library resolver call cannot be cancelled. RelayCore now bounds each lookup, limits concurrent outstanding lookups, and shares one end-to-end deadline across DNS, connection, and response handling. Each action receives at most half its worker lease; the selected public IP remains pinned so the client performs no second DNS lookup.
- Delivery remains at least once. Cancellation cannot stop a request already in flight; a completed response is recorded after cancellation, but duplicate external effects still depend on the remote service honoring the idempotency key.
- URLs and headers remain static. Later HTTP request bodies can select sanitized JSON fields from a successful earlier HTTP response with the bounded `$step` reference defined in ADR 011. Slack text templating, branch conditions, OAuth flows, provider-native adapters, and live third-party endpoint tests remain outside this action contract.
