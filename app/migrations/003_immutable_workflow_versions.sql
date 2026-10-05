CREATE FUNCTION reject_workflow_version_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'workflow versions are immutable';
END;
$$;

CREATE TRIGGER workflow_versions_immutable
BEFORE UPDATE OR DELETE ON workflow_versions
FOR EACH ROW EXECUTE FUNCTION reject_workflow_version_mutation();
