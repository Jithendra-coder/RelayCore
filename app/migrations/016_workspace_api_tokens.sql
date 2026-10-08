CREATE TABLE workspace_api_tokens (
    id UUID PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    token_hash TEXT NOT NULL UNIQUE CHECK (token_hash ~ '^[a-f0-9]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    CHECK (expires_at > created_at)
);

CREATE INDEX workspace_api_tokens_workspace_idx
    ON workspace_api_tokens (workspace_id, created_at DESC);
CREATE INDEX workspace_api_tokens_user_idx
    ON workspace_api_tokens (user_id, expires_at DESC);
