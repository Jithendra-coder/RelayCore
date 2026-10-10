# ADR 011: Bounded HTTP response references

## Status

Accepted.

## Decision

- Allow an exact `{"$step":{"index":0,"pointer":"/body/id"}}` object in an HTTP request body to select a field from an earlier HTTP step result. Step indexes are zero-based; the source must be an earlier HTTP action in the same immutable workflow version.
- Resolve from the stored side-effect result so retries and worker restarts use the same completed response. The result body is the already bounded, parsed, and redacted JSON body stored by the HTTP action.
- Keep the existing 4 KiB rendered request-body limit. A missing result or JSON Pointer is a permanent action failure and goes to the dead-letter queue.
- Keep this as data mapping only. It does not add conditions, branching, or URL/header templates. The separately scoped Slack text reference is defined in [ADR 013](013-bounded-slack-result-references.md).

## Consequences and limits

- A workflow can pass an identifier or other explicitly selected response value to a later HTTP endpoint while every credential remains bound to its configured host.
- Mapping can only use prior HTTP action results. It cannot read an unparsed response, raw headers, secrets redacted from stored results, another workflow's data, or a future step.
- External calls remain at least once. The destination must honor the stable idempotency key because a lost response may cause the same step to be retried.
