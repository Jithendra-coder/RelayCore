# ADR 008: Encrypted workspace integration credentials

## Status

Accepted.

## Decision

- Store named GitHub, Slack, and generic HTTP bearer credentials under a workspace-owned record. Keep immutable secret versions in a separate table.
- Encrypt secret values with the existing Fernet key injected through `RELAYCORE_SECRET_ENCRYPTION_KEY`; persist only ciphertext and a keyed request fingerprint used for idempotency.
- Restrict create, list, rotate, and revoke to signed-in workspace owners and admins. Require an idempotency key for create and rotation. Never return secret values from management endpoints or place them in audit events.
- Resolve credentials internally only by both workspace ID and credential ID. Revoking a credential also revokes its active secret version.

## Consequences and limits

- The encryption master key remains a single environment-injected key. Rotation and multi-key decryption are not implemented.
- Credentials are storage primitives only: provider OAuth lifecycles, outbound adapters, permission scopes, token refresh, provider-side revocation, and UI management remain incomplete.
- Database access without the application key cannot recover plaintext tokens. An application process with key access can decrypt active workspace credentials, so production key access must be limited and audited.
