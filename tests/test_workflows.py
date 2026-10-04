from __future__ import annotations

import json
import threading
import uuid

from psycopg import connect
from psycopg.rows import dict_row

from app.settings import DATABASE_URL
from app.store import claim_task, ingest_business_event
from tests.conftest import ADMIN, OTHER, TEST_TENANT, VIEWER, drive_run, get_run, make_workflow, random_tenant


def test_auth_roles_tenant_boundary_and_request_correlation(client):
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers=VIEWER).status_code == 200
    assert make_workflow(client, tenant_headers=VIEWER).status_code == 403
    response = make_workflow(client)
    assert response.status_code == 202
    run_id = response.json()["id"]
    assert client.get(f"/api/workflows/{run_id}", headers=OTHER).status_code == 404
    correlated = client.get("/api/status", headers={**ADMIN, "X-Request-ID": "build-test-001"})
    assert correlated.headers["X-Request-ID"] == "build-test-001"


def test_idempotent_workflow_creation_and_durable_history(client):
    key = f"persist:{uuid.uuid4()}"
    first = make_workflow(client, key=key)
    again = make_workflow(client, key=key)
    conflict = make_workflow(client, key=key, title="different body")
    assert first.status_code == 202 and first.json()["created"] is True
    assert again.json()["id"] == first.json()["id"] and again.json()["created"] is False
    assert conflict.status_code == 409
    drive_run(client)
    run = get_run(client, first.json()["id"])
    assert run["status"] == "completed"
    assert [event["kind"] for event in run["events"]].count("side_effect.applied") == 2
    with connect(DATABASE_URL, row_factory=dict_row) as conn:
        assert conn.execute("SELECT status FROM workflow_runs WHERE id=%s", (run["id"],)).fetchone()["status"] == "completed"


def test_committed_step_resumes_at_next_step_after_worker_loss(client):
    response = make_workflow(client, steps=[
        {"name": "commit first effect", "action": "record", "payload": {"step": 1}},
        {"name": "continue after worker loss", "action": "record", "payload": {"step": 2}},
    ])
    run_id = response.json()["id"]
    with client.app.state.pool.connection() as conn:
        conn.execute("UPDATE tasks SET available_at=clock_timestamp()-interval '1 year' WHERE run_id=%s", (run_id,))
        first = claim_task(conn, "worker-before-loss", 1.2)
        assert first and first["run_id"] == run_id
        from app.store import execute_step
        execute_step(conn, "worker-before-loss", first, first["request_id"])
        checkpoint = conn.execute(
            "SELECT status,step_index FROM tasks WHERE run_id=%s", (run_id,)
        ).fetchone()
        assert checkpoint == {"status": "queued", "step_index": 1}

        # The first effect and step advancement committed together. A replacement worker
        # continues at step two instead of repeating step one.
        conn.execute("UPDATE tasks SET available_at=clock_timestamp()-interval '1 year' WHERE run_id=%s", (run_id,))
        second = claim_task(conn, "worker-after-loss", 1.2)
        assert second and second["step_index"] == 1
        execute_step(conn, "worker-after-loss", second, second["request_id"])

    run = get_run(client, run_id)
    assert run["status"] == "completed"
    assert [effect["step_index"] for effect in run["side_effects"]] == [0, 1]
    assert sum(event["kind"] == "side_effect.applied" for event in run["events"]) == 2


def test_fail_once_retries_then_completes(client):
    response = make_workflow(client, steps=[{"name": "retry once", "action": "fail_once", "payload": {}}])
    drive_run(client)
    run = get_run(client, response.json()["id"])
    assert run["status"] == "completed"
    kinds = [event["kind"] for event in run["events"]]
    assert "task.retry_scheduled" in kinds
    assert kinds.count("task.claimed") == 2
    assert len(run["side_effects"]) == 1


def test_duplicate_business_event_has_one_workflow_and_one_effect_per_step(client):
    key = f"dup-{uuid.uuid4()}"
    response = client.post("/api/demo/duplicates", headers=ADMIN,
                           json={"event_key": key, "order": "o-123", "amount": 25, "currency": "USD"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["deliveries_received"] == 10 and data["distinct_events"] == 1
    drive_run(client)
    run = get_run(client, data["workflow_id"])
    assert len(run["side_effects"]) == 3
    with client.app.state.pool.connection() as conn:
        event = conn.execute("SELECT received_count FROM business_events WHERE tenant_id=%s AND event_key=%s",
                             (TEST_TENANT, key)).fetchone()
        assert event["received_count"] == 10
        assert conn.execute("SELECT count(*) AS n FROM workflow_runs WHERE tenant_id=%s AND idempotency_key=%s",
                            (TEST_TENANT, f"business-event:{key}")).fetchone()["n"] == 1
    conflict = client.post("/api/demo/duplicates", headers=ADMIN,
                           json={"event_key": key, "order": "changed", "amount": 25, "currency": "USD"})
    assert conflict.status_code == 409


def test_cancel_prevents_an_active_step_side_effect(client):
    response = make_workflow(client, steps=[{"name": "slow", "action": "sleep", "payload": {"seconds": 0.5}}])
    run_id = response.json()["id"]
    with client.app.state.pool.connection() as conn:
        task = claim_task(conn, "cancel-test", 1.2)
    assert task
    cancelled = client.post(f"/api/workflows/{run_id}/cancel", headers=ADMIN)
    assert cancelled.status_code == 200
    with client.app.state.pool.connection() as conn:
        from app.store import execute_step
        execute_step(conn, "cancel-test", task, task["request_id"])
    run = get_run(client, run_id)
    assert run["status"] == "cancelled"
    assert run["side_effects"] == []


def test_dlq_replay_is_admin_only_and_audited(client):
    response = client.post("/api/demo/dlq", headers=ADMIN, json={})
    assert response.status_code == 202
    run_id = response.json()["id"]
    drive_run(client, timeout=8)
    run = get_run(client, run_id)
    assert run["status"] == "failed"
    letters = client.get("/api/dead-letters", headers=ADMIN).json()
    letter = next(row for row in letters if row["run_id"] == run_id)
    assert client.post(f"/api/dead-letters/{letter['id']}/replay", headers=VIEWER).status_code == 403
    replayed = client.post(f"/api/dead-letters/{letter['id']}/replay", headers=ADMIN)
    assert replayed.status_code == 202
    assert get_run(client, run_id)["status"] == "queued"
    drive_run(client, timeout=8)
    run = get_run(client, run_id)
    assert run["status"] == "completed"
    assert sum(e["kind"] == "workflow.replayed" for e in run["events"]) == 1
    assert len(run["side_effects"]) == 1


def test_queue_capacity_is_enforced_per_tenant(client, monkeypatch):
    import app.store as store

    tenant = random_tenant()
    keys = json.loads(__import__("os").environ["RELAYCORE_API_KEYS"])
    secret = f"key-{tenant}-long-enough"
    keys[secret] = {"tenant_id": tenant, "role": "admin"}
    monkeypatch.setenv("RELAYCORE_API_KEYS", json.dumps(keys))
    monkeypatch.setattr(store, "MAX_QUEUE_DEPTH", 1)
    headers = {"Authorization": f"Bearer {secret}"}
    assert make_workflow(client, tenant_headers=headers).status_code == 202
    assert make_workflow(client, tenant_headers=headers).status_code == 429


def test_concurrent_duplicate_delivery_creates_one_logical_event(client):
    tenant, key = random_tenant(), f"race-{uuid.uuid4()}"
    payload = {"order": "racing-order", "amount": 12, "currency": "USD"}
    barrier = threading.Barrier(2)
    results = []

    def deliver():
        with connect(DATABASE_URL, row_factory=dict_row, autocommit=True) as conn:
            barrier.wait()
            with conn.transaction():
                results.append(ingest_business_event(conn, tenant, key, payload, "race-test"))

    threads = [threading.Thread(target=deliver) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert len(results) == 2
    assert sum(result["created"] for result in results) == 1
    assert len({result["workflow_id"] for result in results}) == 1
    with client.app.state.pool.connection() as conn:
        event = conn.execute("SELECT received_count FROM business_events WHERE tenant_id=%s AND event_key=%s",
                             (tenant, key)).fetchone()
        assert event["received_count"] == 2
