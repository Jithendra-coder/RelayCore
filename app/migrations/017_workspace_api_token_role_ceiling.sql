ALTER TABLE workspace_api_tokens
    ADD COLUMN role_ceiling TEXT NOT NULL DEFAULT 'admin'
    CHECK (role_ceiling IN ('viewer', 'operator', 'admin'));
