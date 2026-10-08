# ADR 005: Encrypted webhook signing secrets

## Status

Accepted for custom webhook credentials.

## Decision

- Generate a 256-bit random HMAC secret per endpoint and return it only when the endpoint is created or rotated. Persist only Fernet-encrypted secret bytes in PostgreSQL.
- Require `RELAYCORE_SECRET_ENCRYPTION_KEY` to be injected from a secret manager for production startup. Never put it in source control or the database.
- Rotate the database encryption key with `python -m scripts.rotate_secrets` during maintenance. The command atomically re-encrypts stored values and recalculates key-derived credential fingerprints; services must remain stopped until every instance uses the new key.
- Sign the exact raw JSON body together with the timestamp and event ID. Verify HMAC-SHA256 with constant-time comparison and accept timestamps only within five minutes.
- Store each accepted event under a unique `(endpoint_id, event_key)`. An identical replay returns the prior result; the same event ID with changed bytes returns 409.
- Record endpoint create/rotate/revoke events without including the secret or payload.

## Consequences and limits

- Losing the active key makes persisted webhook and integration secrets unreadable. Rotation is an explicit offline transaction, not a zero-downtime multi-key rollout. Old keys remain necessary for pre-rotation database backups until those backups expire or are re-encrypted.
- Fernet protects database contents when the application key remains outside PostgreSQL. Production still requires secret-manager access controls and disk/database encryption.
- Endpoint secret rotation revokes the prior signing key immediately. The caller must install the new key before sending further events.
- Exact event triggers are supported in production for constrained versioned workflows. Raw webhook bodies and parsed JSON expire after a configurable period; metadata/dedupe retention, per-endpoint JSON schemas, and edge IP rate limits remain open work.
