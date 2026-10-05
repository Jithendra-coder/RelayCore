CREATE TABLE webhook_endpoints (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 100),
    created_by TEXT NOT NULL REFERENCES users(id),
    creation_key TEXT NOT NULL,
    creation_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    revoked_at TIMESTAMPTZ,
    last_received_at TIMESTAMPTZ,
    UNIQUE (workspace_id, creation_key)
);

CREATE INDEX webhook_endpoints_workspace_idx
    ON webhook_endpoints (workspace_id, created_at DESC);

CREATE TABLE webhook_secrets (
    endpoint_id TEXT NOT NULL REFERENCES webhook_endpoints(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    encrypted_secret TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    revoked_at TIMESTAMPTZ,
    PRIMARY KEY (endpoint_id, version),
    UNIQUE (endpoint_id, idempotency_key)
);

CREATE UNIQUE INDEX webhook_one_active_secret_idx
    ON webhook_secrets (endpoint_id) WHERE revoked_at IS NULL;

CREATE TABLE incoming_events (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    endpoint_id TEXT NOT NULL REFERENCES webhook_endpoints(id),
    event_key TEXT NOT NULL,
    request_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    raw_body BYTEA NOT NULL,
    payload JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (endpoint_id, event_key)
);

CREATE INDEX incoming_events_workspace_received_idx
    ON incoming_events (workspace_id, received_at DESC);
