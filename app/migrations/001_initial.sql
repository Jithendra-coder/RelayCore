CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    title TEXT NOT NULL,
    definition JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued','running','retrying','completed','failed','cancelled')),
    idempotency_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at TIMESTAMPTZ,
    UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES workflow_runs(id),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','retry_wait','succeeded','dead','cancelled')),
    step_index INTEGER NOT NULL DEFAULT 0 CHECK (step_index >= 0),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 10),
    available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    last_worker TEXT,
    request_id TEXT NOT NULL,
    last_error JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id)
);
ALTER TABLE tasks ALTER COLUMN status SET DEFAULT 'queued';

CREATE TABLE IF NOT EXISTS events (
    sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    run_id TEXT,
    task_id TEXT,
    worker_id TEXT,
    request_id TEXT,
    kind TEXT NOT NULL,
    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS business_events (
    tenant_id TEXT NOT NULL,
    event_key TEXT NOT NULL,
    received_count INTEGER NOT NULL DEFAULT 1 CHECK (received_count > 0),
    run_id TEXT,
    payload JSONB NOT NULL,
    first_received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, event_key)
);

CREATE TABLE IF NOT EXISTS side_effects (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS dead_letters (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    error JSONB NOT NULL,
    failed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    replayed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    host TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    stopped_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS rate_limits (
    tenant_id TEXT NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    request_count INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, window_start)
);

CREATE INDEX IF NOT EXISTS tasks_ready_idx ON tasks (available_at, created_at)
    WHERE status IN ('queued','retry_wait');
CREATE INDEX IF NOT EXISTS tasks_tenant_status_idx ON tasks (tenant_id,status)
    WHERE status IN ('queued','retry_wait','running');
CREATE INDEX IF NOT EXISTS tasks_expired_lease_idx ON tasks (lease_until)
    WHERE status = 'running';
CREATE INDEX IF NOT EXISTS events_tenant_sequence_idx ON events (tenant_id, sequence DESC);
CREATE INDEX IF NOT EXISTS events_run_sequence_idx ON events (tenant_id, run_id, sequence);
CREATE INDEX IF NOT EXISTS runs_tenant_created_idx ON workflow_runs (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS dead_letters_tenant_failed_idx ON dead_letters (tenant_id, failed_at DESC);
