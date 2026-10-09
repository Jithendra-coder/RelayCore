# ADR 012: Scoped GitHub pull-request comments

## Status

Accepted.

## Decision

- Add one GitHub App action: create a comment on an issue or pull request. Keep the API host and endpoint fixed; accept only an owner/repository, positive issue number, and bounded comment body.
- Require an active workspace GitHub App installation. A workflow trigger using this action must use that installation's private pull-request webhook endpoint and a normalized pull-request event.
- Permit exact `$event` JSON Pointer references for the repository, issue number, and comment body. Validate resolved values before making a request and require the repository owner to match the linked installation account.
- Request a short-lived installation token for only the selected repository and `Issues: write`. Do not persist or return the token. Store only the comment ID and validated GitHub URL.
- Share the worker's half-lease time budget across token creation and comment creation; cap response reads at 1 MB and disable redirects.

## Consequences and limits

- The GitHub App must grant **Issues: write** in addition to the read-only pull-request permission used by webhook intake.
- Primary and secondary rate-limit responses are retryable. RelayCore honors `Retry-After`; when GitHub reports a secondary limit without that header, it waits at least 60 seconds before retrying.
- Delivery remains at least once. RelayCore does not send an idempotency key for this action, so a lost response can result in a duplicate comment after retry.
- Only comment creation is supported. Labels, checks, releases, merges, issue edits, and live-provider verification remain out of scope for this action.
