CREATE TABLE integration_credentials (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    provider TEXT NOT NULL CHECK (provider IN ('github', 'slack', 'http')),
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 100),
    created_by TEXT NOT NULL REFERENCES users(id),
    creation_key TEXT NOT NULL,
    creation_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    revoked_at TIMESTAMPTZ,
    UNIQUE (workspace_id, creation_key)
);

CREATE INDEX integration_credentials_workspace_idx
    ON integration_credentials (workspace_id, created_at DESC);

CREATE TABLE integration_credential_secrets (
    credential_id TEXT NOT NULL REFERENCES integration_credentials(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    encrypted_secret TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    secret_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    revoked_at TIMESTAMPTZ,
    PRIMARY KEY (credential_id, version),
    UNIQUE (credential_id, idempotency_key)
);

CREATE UNIQUE INDEX integration_credential_one_active_secret_idx
    ON integration_credential_secrets (credential_id) WHERE revoked_at IS NULL;
