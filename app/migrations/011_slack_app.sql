ALTER TABLE webhook_endpoints DROP CONSTRAINT webhook_endpoints_source_check;
ALTER TABLE webhook_endpoints
    ADD CONSTRAINT webhook_endpoints_source_check CHECK (source IN ('custom', 'github', 'slack'));

CREATE TABLE slack_oauth_states (
    state_hash TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp() + interval '10 minutes',
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX slack_oauth_states_expiry_idx ON slack_oauth_states (expires_at);

CREATE TABLE slack_installations (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL CHECK (length(team_id) BETWEEN 1 AND 64),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    endpoint_id TEXT NOT NULL UNIQUE REFERENCES webhook_endpoints(id),
    credential_id TEXT NOT NULL REFERENCES integration_credentials(id),
    team_name TEXT NOT NULL CHECK (length(team_name) BETWEEN 1 AND 100),
    bot_user_id TEXT NOT NULL CHECK (length(bot_user_id) BETWEEN 1 AND 64),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    linked_by TEXT NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE UNIQUE INDEX slack_installation_one_active_workspace_idx
    ON slack_installations (workspace_id) WHERE status = 'active';
CREATE UNIQUE INDEX slack_installation_one_active_team_idx
    ON slack_installations (team_id) WHERE status = 'active';
