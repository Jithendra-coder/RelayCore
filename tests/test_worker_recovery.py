from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import psycopg
from psycopg.rows import dict_row
from app.supervisor import WorkerSupervisor
from tests.conftest import ADMIN, get_run, make_workflow


def test_external_worker_has_unique_safe_identity(monkeypatch):
    import pytest
    import app.worker as worker

    monkeypatch.delenv("RELAYCORE_WORKER_ID", raising=False)
    monkeypatch.setattr(worker.socket, "gethostname", lambda: "worker-node")
    monkeypatch.setattr(worker.os, "getpid", lambda: 1234)
    assert worker.worker_identifier() == "worker-worker-node-1234"
    monkeypatch.setenv("RELAYCORE_WORKER_ID", 'bad"\nworker')
    with pytest.raises(RuntimeError, match="safe identifier"):
        worker.worker_identifier()


def test_external_worker_process_claims_work_without_api_supervisor(client):
    worker_id = f"external-{uuid.uuid4().hex[:12]}"
    environment = os.environ.copy()
    environment["RELAYCORE_WORKER_ID"] = worker_id
    process = subprocess.Popen([sys.executable, "-m", "app.worker"],
                               cwd=os.path.dirname(os.path.dirname(__file__)), env=environment,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        response = make_workflow(client, steps=[
            {"name": "processed by the standalone service", "action": "record", "payload": {"ok": True}},
        ])
        run_id = response.json()["id"]
        deadline = time.monotonic() + 8
        run = get_run(client, run_id)
        while run["status"] not in {"completed", "failed", "cancelled"} and time.monotonic() < deadline:
            time.sleep(0.05)
            run = get_run(client, run_id)
        assert run["status"] == "completed"
        assert any(event["worker_id"] == worker_id for event in run["events"] if event["kind"] == "task.claimed")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_real_worker_process_kill_expires_lease_and_reassigns(client):
    supervisor = WorkerSupervisor(2)
    existing_supervisor = client.app.state.supervisor
    client.app.state.supervisor = supervisor
    supervisor.start()
    try:
        response = make_workflow(client, steps=[
            {"name": "interrupted operation", "action": "sleep", "payload": {"seconds": 2.2}},
            {"name": "charge once after recovery", "action": "charge", "payload": {"amount": 15, "currency": "USD"}},
        ])
        run_id = response.json()["id"]
        deadline = time.monotonic() + 5
        lease = None
        while time.monotonic() < deadline:
            with client.app.state.pool.connection() as conn:
                lease = conn.execute("SELECT lease_owner FROM tasks WHERE run_id=%s AND status='running'", (run_id,)).fetchone()
            if lease and lease["lease_owner"]:
                break
            time.sleep(0.05)
        assert lease and lease["lease_owner"]
        killed = lease["lease_owner"]
        response = client.post(f"/api/workers/{killed}/kill", headers=ADMIN)
        assert response.status_code == 200, response.text
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with client.app.state.pool.connection() as conn:
                    connection = conn.execute("SELECT count(*) AS n FROM pg_stat_activity WHERE application_name=%s AND datname=current_database()",
                                          (f"relaycore-{killed}",)).fetchone()
            if connection["n"] == 0:
                break
            time.sleep(0.05)
        assert connection["n"] == 0, "The worker's database child process remained alive after termination."
        deadline = time.monotonic() + 12
        run = None
        while time.monotonic() < deadline:
            run = get_run(client, run_id)
            if run["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.08)
        run = get_run(client, run_id)
        assert run["status"] == "completed"
        assert "task.lease_expired" in [event["kind"] for event in run["events"]]
        claims = [event["worker_id"] for event in run["events"] if event["kind"] == "task.claimed"]
        assert killed in claims and len(set(claims)) >= 2
        assert len(run["side_effects"]) == 2
    finally:
        client.app.state.supervisor = existing_supervisor
        supervisor.close()


def test_worker_reconnects_after_postgres_drops_its_connection(client):
    supervisor = WorkerSupervisor(1)
    supervisor.start()
    try:
        response = make_workflow(client, steps=[
            {"name": "active when DB connection is reset", "action": "sleep", "payload": {"seconds": 2.2}},
            {"name": "finish after reconnection", "action": "record", "payload": {"ok": True}},
        ])
        run_id = response.json()["id"]
        deadline = time.monotonic() + 5
        backend = None
        while time.monotonic() < deadline:
            with client.app.state.pool.connection() as conn:
                backend = conn.execute(
                    "SELECT pid FROM pg_stat_activity WHERE application_name='relaycore-worker-1' AND datname=current_database()"
                ).fetchone()
            if backend:
                state = get_run(client, run_id)
                if state["task_status"] == "running":
                    break
            time.sleep(0.05)
        assert backend
        with psycopg.connect(client.app.state.pool.conninfo, row_factory=dict_row, autocommit=True) as conn:
            terminated = conn.execute("SELECT pg_terminate_backend(%s) AS terminated", (backend["pid"],)).fetchone()
        assert terminated["terminated"] is True
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            run = get_run(client, run_id)
            if run["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.08)
        run = get_run(client, run_id)
        assert run["status"] == "completed"
        assert "task.lease_expired" in [event["kind"] for event in run["events"]]
        assert len(run["side_effects"]) == 2
        assert supervisor.state()["worker-1"] is True
    finally:
        supervisor.close()


def test_api_pool_replaces_idle_connections_lost_with_postgres(client):
    pool = client.app.state.pool
    with psycopg.connect(pool.conninfo, row_factory=dict_row, autocommit=True) as killer:
        backends = killer.execute(
            "SELECT pid FROM pg_stat_activity WHERE application_name='relaycore-api' AND datname=current_database()"
        ).fetchall()
        assert backends
        for backend in backends:
            terminated = killer.execute(
                "SELECT pg_terminate_backend(%s) AS terminated", (backend["pid"],)
            ).fetchone()["terminated"]
            assert terminated

    # Every pooled socket was severed while idle; checkout must discard it and reconnect.
    with pool.connection() as conn:
        assert conn.execute("SELECT 1 AS healthy").fetchone()["healthy"] == 1
        new_pid = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        assert new_pid not in {row["pid"] for row in backends}

    response = client.get("/healthz")
    assert response.status_code == 200, response.text
