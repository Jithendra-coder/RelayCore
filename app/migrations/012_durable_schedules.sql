ALTER TABLE workflow_runs ADD CONSTRAINT workflow_runs_tenant_id_id_key UNIQUE (tenant_id, id);

CREATE TABLE workflow_schedules (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 100),
    interval_seconds INTEGER NOT NULL CHECK (interval_seconds BETWEEN 60 AND 31536000),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','cancelled')),
    next_run_at TIMESTAMPTZ NOT NULL,
    last_run_at TIMESTAMPTZ,
    last_run_id TEXT,
    pause_reason TEXT,
    idempotency_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_by TEXT REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, workflow_id)
        REFERENCES workflow_definitions (tenant_id, id),
    FOREIGN KEY (tenant_id, last_run_id)
        REFERENCES workflow_runs (tenant_id, id)
);

CREATE INDEX workflow_schedules_due_idx ON workflow_schedules (next_run_at, id)
    WHERE status = 'active';
