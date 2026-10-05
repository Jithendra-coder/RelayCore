# ADR 005: Encrypted webhook signing secrets

## Status

Accepted for custom webhook credentials.

## Decision

- Generate a 256-bit random HMAC secret per endpoint and return it only when the endpoint is created or rotated. Persist only Fernet-encrypted secret bytes in PostgreSQL.
- Require `RELAYCORE_SECRET_ENCRYPTION_KEY` to be injected from a secret manager for production startup. Never put it in source control or the database.
- Sign the exact raw JSON body together with the timestamp and event ID. Verify HMAC-SHA256 with constant-time comparison and accept timestamps only within five minutes.
- Store each accepted event under a unique `(endpoint_id, event_key)`. An identical replay returns the prior result; the same event ID with changed bytes returns 409.
- Record endpoint create/rotate/revoke events without including the secret or payload.

## Consequences and limits

- Losing or changing the encryption key makes persisted webhook secrets unreadable. Master-key rotation currently requires an offline decrypt/re-encrypt operation and is not automated.
- Fernet protects database contents when the application key remains outside PostgreSQL. Production still requires secret-manager access controls and disk/database encryption.
- Endpoint secret rotation revokes the prior signing key immediately. The caller must install the new key before sending further events.
- Event trigger matching exists only in Demo Mode and is documented in ADR 007. Per-endpoint JSON schemas, retention/cleanup, and edge IP rate limits remain future work.
