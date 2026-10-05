CREATE TABLE workflow_definitions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
    current_version INTEGER NOT NULL DEFAULT 1 CHECK (current_version >= 1),
    idempotency_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    author_credential_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE workflow_versions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number >= 1),
    definition JSONB NOT NULL,
    definition_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    author_credential_fingerprint TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, workflow_id, version_number),
    UNIQUE (tenant_id, workflow_id, idempotency_key),
    FOREIGN KEY (tenant_id, workflow_id)
        REFERENCES workflow_definitions (tenant_id, id)
);

ALTER TABLE workflow_runs
    ADD COLUMN workflow_version_id TEXT,
    ADD COLUMN definition_hash TEXT;

ALTER TABLE workflow_runs
    ADD CONSTRAINT workflow_runs_tenant_version_fk
    FOREIGN KEY (tenant_id, workflow_version_id)
    REFERENCES workflow_versions (tenant_id, id);

CREATE INDEX workflow_definitions_tenant_updated_idx
    ON workflow_definitions (tenant_id, updated_at DESC);
CREATE INDEX workflow_versions_tenant_workflow_idx
    ON workflow_versions (tenant_id, workflow_id, version_number DESC);
