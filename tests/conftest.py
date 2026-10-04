from __future__ import annotations

import json
import os
import time
import uuid

import pytest

TEST_TENANT = f"relaycore-test-{uuid.uuid4().hex}"
OTHER_TENANT = f"relaycore-other-{uuid.uuid4().hex}"
os.environ.setdefault("DATABASE_URL", "postgresql://postgres@127.0.0.1:55433/postgres")
os.environ["RELAYCORE_DEMO_MODE"] = "1"
os.environ["RELAYCORE_WORKERS"] = "0"
os.environ["RELAYCORE_LEASE_SECONDS"] = "1.2"
os.environ["RELAYCORE_API_KEYS"] = json.dumps({
    "demo-key-change-me-32": {"tenant_id": TEST_TENANT, "role": "admin"},
    "viewer-key-change-me-32": {"tenant_id": TEST_TENANT, "role": "viewer"},
    "other-key-change-me-32": {"tenant_id": OTHER_TENANT, "role": "admin"},
})

from fastapi.testclient import TestClient

from app.main import app
from app.store import claim_task, execute_step, fail_task, run_summary

ADMIN = {"Authorization": "Bearer demo-key-change-me-32"}
VIEWER = {"Authorization": "Bearer viewer-key-change-me-32"}
OTHER = {"Authorization": "Bearer other-key-change-me-32"}


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


def make_workflow(client: TestClient, steps=None, *, tenant_headers=ADMIN, key=None, title="integration flow"):
    body = {"title": title, "steps": steps or [
        {"name": "record one", "action": "record", "payload": {"value": 1}},
        {"name": "record two", "action": "record", "payload": {"value": 2}},
    ]}
    headers = {**tenant_headers, "Idempotency-Key": key or f"test:{uuid.uuid4()}"}
    return client.post("/api/workflows", headers=headers, json=body)


def drive_run(client: TestClient, tenant_id=TEST_TENANT, worker_id="test-worker", timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with client.app.state.pool.connection() as conn:
            pending = conn.execute(
                "SELECT id FROM workflow_runs WHERE tenant_id=%s AND status NOT IN ('completed','failed','cancelled') ORDER BY created_at",
                (tenant_id,),
            ).fetchall()
            if not pending:
                return
            task = claim_task(conn, worker_id, 1.2)
            if task:
                try:
                    execute_step(conn, worker_id, task, task["request_id"])
                except Exception as exc:
                    fail_task(conn, task, worker_id, exc, task["request_id"])
                continue
        time.sleep(0.04)
    raise AssertionError("workflow did not reach a terminal state")


def get_run(client: TestClient, run_id: str, tenant_id=TEST_TENANT):
    with client.app.state.pool.connection() as conn:
        return run_summary(conn, tenant_id, run_id)


def random_tenant() -> str:
    return f"test-{uuid.uuid4().hex[:12]}"
