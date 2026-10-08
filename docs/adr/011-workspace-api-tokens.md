# ADR 011: Workspace-bound API tokens

- Status: accepted
- Date: 2026-10-08

## Context

RelayCore's production API uses short-lived OIDC session cookies. A typed client or CLI needs a non-browser credential, while the static bearer keys are deliberately limited to Demo Mode. The credential must not let a workspace client cross tenant boundaries or keep access after its user loses membership.

## Decision

Workspace owners and admins can issue named API tokens from an authenticated dashboard session. Each token is bound to one workspace and user, expires within 1 to 90 days, and is shown only in the creation response. RelayCore stores a SHA-256 hash of the 256-bit random secret, records creation/revocation events without the secret, and checks expiry/revocation on every bearer request. Workspace membership and role are looked up for each request, so role changes and membership removal apply immediately. Token issuance uses the existing workspace write limiter.

The token grants the user's full current role in that one workspace. Fine-grained token scopes and refresh tokens are not included. Revocation followed by issuance replaces a token. Static Demo Mode keys keep their existing behavior and are not accepted as production tokens.

## Consequences

- The SDK/CLI can authenticate to the production API without extracting a browser cookie.
- A database read on each authenticated request allows immediate revocation, expiry, membership, and role enforcement.
- Only a high-entropy hash is stored; database read access does not reveal a usable API secret.
- A leaked token can perform all actions available to its user's current role until it expires or is revoked. Operators should keep it in a secret manager and choose the shortest practical lifetime.
- The design is workspace-specific; account-wide access and cross-workspace automation require separate tokens.
