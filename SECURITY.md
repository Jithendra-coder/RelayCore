# Security model

## Controls implemented

- Production startup requires complete HTTPS OIDC configuration. Authlib handles discovery, authorization-code exchange, and ID-token verification. RelayCore requires the configured issuer, a non-empty subject, and a verified email claim.
- The browser receives an opaque `HttpOnly`, `Secure`, `SameSite=Lax` session cookie. PostgreSQL stores only its SHA-256 hash; sessions expire and can be revoked. Unsafe cookie-authenticated requests require the configured same-origin `Origin`.
- Production requests resolve a selected workspace through an active membership. Cross-workspace resources return not found. Role checks distinguish owners, admins, developers, and viewers. Membership writes preserve an owner and prevent admins from changing another owner's membership.
- SQL uses parameterized Psycopg statements; workflow steps are validated against a fixed action allow-list. No workflow payload is evaluated as Python or shell.
- Webhook signing keys are random and Fernet-encrypted in PostgreSQL. Plaintext is returned only at endpoint creation or rotation. HMAC-SHA256 covers the timestamp, event ID, and exact raw body; constant-time comparison, a five-minute freshness window, a 256 KiB body cap, and a unique event key prevent forged or duplicate intake.
- Workspace bearer credentials for GitHub, Slack, and generic HTTP are Fernet-encrypted in PostgreSQL. Owners/admins can create, rotate, list metadata, and revoke them. APIs and audit events never return or log the secret; internal retrieval requires both workspace and credential IDs.
- Production HTTP steps require a credential bound to the exact operator-allowlisted DNS hostname, HTTPS on port 443, and public DNS answers. RelayCore pins the validated address while TLS verifies the original hostname; redirects are not followed. Request/response sizes and timeouts are bounded, credential-like headers/query parameters are rejected, and sensitive JSON response fields are redacted.
- Production webhook intake stores the signed event and creates matching HTTP-only runs in one database transaction. The run references the event row; workers resolve explicit JSON Pointer references only in the request body. The event payload stays out of run-history API responses and audit metadata.
- Structured HTTP logs use request IDs and do not include authorization headers or session values. User-visible dashboard values are escaped before insertion into HTML.
- Request-validation responses include field locations and messages but omit submitted values and validator context, avoiding accidental echo of secrets or webhook payloads.
- Static API keys and simulated provider actions are limited to Demo Mode.
- The worker repeats the Demo/production action-mode check before dispatch. Old queued sandbox actions opened under production settings fail permanently into the DLQ without creating an effect.
- Demo action implementations are isolated in `app/sandbox.py`, which production workers do not import.

## Unimplemented protections

Provider OAuth lifecycles/provider-side token revocation, provider-native actions, URL/header templating, HTTP response mapping, and non-HTTP production actions are not implemented. The HTTP action allowlist is an operator control and must be kept narrow. OS-level DNS lookup can block beyond the configured socket timeout; a resolver outage may outlive a task lease, so external providers should enforce the supplied idempotency key. The encryption key has no automated master-key rotation, and incoming payload retention is unbounded. Per-endpoint JSON schemas, edge IP rate limits, invitation flows, MFA/account recovery, automated dependency scanning, and a live third-party security review are pending.

Production must use a managed PostgreSQL service, TLS at ingress and database connections, a secret manager, network restrictions, backups, and a reviewed OIDC provider configuration. Demo Mode is not a shared production security boundary.
