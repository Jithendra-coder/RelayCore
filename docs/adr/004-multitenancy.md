# ADR 004: OIDC identities and PostgreSQL workspaces

## Status

Accepted for the first identity and workspace implementation.

## Decision

- Use an external OpenID Connect provider for sign-in. Identify an account by the verified `(issuer, subject)` pair; email is a verified contact attribute, never an account-linking key.
- Keep users, workspaces, memberships, and revocable server-side sessions in PostgreSQL. Workspaces own the existing tenant-scoped data; every API operation resolves a workspace membership before using its ID as `tenant_id`.
- Store only a hash of a random session cookie in PostgreSQL. Cookies are `HttpOnly`, `Secure`, and `SameSite=Lax`; unsafe cookie-authenticated requests must have the configured same-origin `Origin`.
- Use workspace roles `OWNER`, `ADMIN`, `DEVELOPER`, and `VIEWER`. Demo API keys remain available only in isolated Demo Mode. Production startup requires complete OIDC configuration.
- Use Authlib's Starlette OIDC client for provider discovery, authorization-code exchange, and ID-token verification rather than implementing OAuth or JWT validation locally.

## Consequences

- An OIDC issuer, client, redirect URI, state-signing secret, and session secret are required before non-demo service startup. The user's deployment account is not required to build or run local tests.
- The first authenticated user can create a workspace and becomes its owner. Workspace members must already have signed in once before an owner/admin can add them; email invitations and provider-specific account linking are later work.
- OAuth scopes are limited to `openid email profile`. Provider API tokens are not retained or used as integration credentials.
- API-key service credentials, SSO policy, MFA, account recovery, and a live external-provider security review remain separate requirements.

## Acceptance gate

1. Validated OIDC identity is persisted by issuer and subject; unverified/missing email is rejected.
2. A user can create a workspace and becomes its owner; membership role changes are constrained and audited.
3. Every existing workspace-scoped route resolves `X-Workspace-ID` through an active membership. Cross-workspace reads and writes fail closed.
4. Sessions expire and revoke server-side; only a token hash is stored; cookie-authenticated state changes reject a missing/wrong Origin.
5. Existing Demo Mode workflows still pass their suite; database upgrade from migration 003 and the role/cross-workspace tests pass.
6. Provider credentials are not present in this repository, so live OIDC-provider interoperability remains a separate deployment check and must be reported as unverified.
