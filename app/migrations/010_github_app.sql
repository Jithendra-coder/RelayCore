ALTER TABLE webhook_endpoints
    ADD COLUMN source TEXT NOT NULL DEFAULT 'custom' CHECK (source IN ('custom', 'github'));

CREATE TABLE github_oauth_states (
    state_hash TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    verifier_encrypted TEXT NOT NULL,
    installation_id BIGINT CHECK (installation_id > 0),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp() + interval '10 minutes',
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX github_oauth_states_expiry_idx ON github_oauth_states (expires_at);

CREATE TABLE github_installations (
    installation_id BIGINT PRIMARY KEY CHECK (installation_id > 0),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    endpoint_id TEXT NOT NULL UNIQUE REFERENCES webhook_endpoints(id),
    account_id BIGINT NOT NULL CHECK (account_id > 0),
    account_login TEXT NOT NULL CHECK (length(account_login) BETWEEN 1 AND 100),
    account_type TEXT NOT NULL CHECK (account_type IN ('User', 'Organization')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'revoked')),
    linked_by TEXT NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (workspace_id)
);

CREATE INDEX github_installations_workspace_idx ON github_installations (workspace_id, created_at DESC);
