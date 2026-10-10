# ADR 013: Bounded Slack result references

## Status

Accepted.

## Decision

- Permit Slack message `text` to be either a static string or one exact `{"$step":{"index":0,"pointer":"/body/summary"}}` reference to a successful earlier HTTP step in the same immutable workflow version.
- Resolve the reference from the persisted sanitized side-effect result for the same tenant and run. The selected value must be a printable string of 1–4000 characters before RelayCore opens a Slack connection.
- Keep the Slack conversation ID fixed in the step payload. Do not add interpolation, dynamic channel selection, event references, or access to raw responses, headers, or secret values.
- Preserve the existing at-least-once delivery contract; a successful Slack post may be repeated after an ambiguous failure.

## Consequences and limits

- A workflow can announce a status or other string returned by an earlier HTTP action without adding general-purpose templates or exposing credentials to workflow authors.
- Workflow validation rejects self, forward, and non-HTTP references. Missing pointers and non-string or out-of-range rendered values are permanent action failures.
- Slack API behavior remains covered by mocked responses. Live provider interoperability is unverified until a configured Slack workspace is exercised.
