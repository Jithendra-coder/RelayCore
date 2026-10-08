ALTER TABLE incoming_events ALTER COLUMN raw_body DROP NOT NULL;
ALTER TABLE incoming_events ALTER COLUMN payload DROP NOT NULL;
ALTER TABLE incoming_events ADD COLUMN event_type TEXT;

UPDATE incoming_events SET event_type=left(payload->>'type',120);

CREATE INDEX incoming_events_payload_expiry_idx
    ON incoming_events (received_at, id) WHERE payload IS NOT NULL;

CREATE INDEX workflow_runs_trigger_event_idx
    ON workflow_runs (tenant_id, trigger_event_id) WHERE trigger_event_id IS NOT NULL;
