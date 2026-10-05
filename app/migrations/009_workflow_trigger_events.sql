ALTER TABLE workflow_runs
    ADD COLUMN trigger_event_id TEXT REFERENCES incoming_events(id);
