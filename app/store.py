from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import uuid
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.settings import MAX_ATTEMPTS, MAX_QUEUE_DEPTH, RATE_LIMIT_PER_MINUTE


class QueueFull(Exception):
    pass


class RateLimited(Exception):
    pass


class IdempotencyConflict(Exception):
    pass


def migrate(conn: Connection) -> None:
    from pathlib import Path

    for statement in Path(__file__).with_name("schema.sql").read_text(encoding="utf-8").split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.execute("INSERT INTO schema_migrations(version) VALUES ('001') ON CONFLICT DO NOTHING")


def emit_event(
    conn: Connection,
    tenant_id: str,
    kind: str,
    *,
    run_id: str | None = None,
    task_id: str | None = None,
    worker_id: str | None = None,
    request_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """INSERT INTO events(tenant_id,run_id,task_id,worker_id,request_id,kind,data)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (tenant_id, run_id, task_id, worker_id, request_id, kind, Jsonb(data or {})),
    )


def _admit(conn: Connection, tenant_id: str) -> None:
    conn.execute(
        "DELETE FROM rate_limits WHERE window_start<date_trunc('minute',clock_timestamp())-interval '1 hour'"
    )
    rate = conn.execute(
        """INSERT INTO rate_limits(tenant_id,window_start,request_count)
           VALUES (%s,date_trunc('minute',clock_timestamp()),1)
           ON CONFLICT (tenant_id,window_start) DO UPDATE
             SET request_count=rate_limits.request_count+1
           RETURNING request_count""",
        (tenant_id,),
    ).fetchone()["request_count"]
    if rate > RATE_LIMIT_PER_MINUTE:
        raise RateLimited
    pending = conn.execute(
        "SELECT count(*) AS count FROM tasks WHERE tenant_id=%s AND status IN ('queued','retry_wait','running')",
        (tenant_id,),
    ).fetchone()["count"]
    if pending >= MAX_QUEUE_DEPTH:
        raise QueueFull


def create_workflow(
    conn: Connection,
    tenant_id: str,
    title: str,
    steps: list[dict[str, Any]],
    idempotency_key: str,
    request_id: str,
) -> dict[str, Any]:
    fingerprint = hashlib.sha256(
        json.dumps({"title": title, "steps": steps}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    existing = conn.execute(
        "SELECT id,fingerprint,status FROM workflow_runs WHERE tenant_id=%s AND idempotency_key=%s",
        (tenant_id, idempotency_key),
    ).fetchone()
    if existing:
        if existing["fingerprint"] != fingerprint:
            raise IdempotencyConflict
        return {"id": existing["id"], "status": existing["status"], "created": False}

    _admit(conn, tenant_id)
    run_id, task_id = str(uuid.uuid4()), str(uuid.uuid4())
    definition = {"title": title, "steps": steps}
    conn.execute(
        """INSERT INTO workflow_runs(id,tenant_id,title,definition,status,idempotency_key,fingerprint)
           VALUES (%s,%s,%s,%s,'queued',%s,%s)""",
        (run_id, tenant_id, title, Jsonb(definition), idempotency_key, fingerprint),
    )
    conn.execute(
        "INSERT INTO tasks(id,tenant_id,run_id,max_attempts,request_id) VALUES (%s,%s,%s,%s,%s)",
        (task_id, tenant_id, run_id, MAX_ATTEMPTS, request_id),
    )
    emit_event(conn, tenant_id, "workflow.created", run_id=run_id, task_id=task_id,
               request_id=request_id, data={"title": title, "steps": len(steps)})
    return {"id": run_id, "task_id": task_id, "status": "queued", "created": True}


def ingest_business_event(
    conn: Connection,
    tenant_id: str,
    event_key: str,
    payload: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    inserted = conn.execute(
        """INSERT INTO business_events(tenant_id,event_key,payload)
           VALUES (%s,%s,%s) ON CONFLICT DO NOTHING RETURNING event_key""",
        (tenant_id, event_key, Jsonb(payload)),
    ).fetchone()
    if inserted:
        run = create_workflow(
            conn,
            tenant_id,
            "Duplicate-safe order payment",
            [
                {"name": "reserve inventory", "action": "record", "payload": {"order": payload.get("order", "demo-order")}},
                {"name": "charge payment", "action": "charge", "payload": {"amount": payload.get("amount", 25), "currency": payload.get("currency", "USD")}},
                {"name": "confirm shipment", "action": "record", "payload": {"order": payload.get("order", "demo-order")}},
            ],
            f"business-event:{event_key}",
            request_id,
        )
        conn.execute(
            "UPDATE business_events SET run_id=%s WHERE tenant_id=%s AND event_key=%s",
            (run["id"], tenant_id, event_key),
        )
        received_count, created = 1, run["created"]
    else:
        row = conn.execute(
            """UPDATE business_events SET received_count=received_count+1,
                      last_received_at=clock_timestamp()
               WHERE tenant_id=%s AND event_key=%s RETURNING received_count,run_id,payload""",
            (tenant_id, event_key),
        ).fetchone()
        if row["payload"] != payload:
            raise IdempotencyConflict
        received_count, created = row["received_count"], False
        run = {"id": row["run_id"]}

    emit_event(conn, tenant_id, "business_event.received", run_id=run["id"], request_id=request_id,
               data={"event_key": event_key, "received_count": received_count, "workflow_created": created})
    return {"event_key": event_key, "received": received_count, "workflow_id": run["id"], "created": created}


def claim_task(conn: Connection, worker_id: str, lease_seconds: float) -> dict[str, Any] | None:
    with conn.transaction():
        selection = """SELECT t.id,t.tenant_id,t.run_id,t.step_index,t.attempts,t.max_attempts,
                          t.last_worker,t.request_id,w.title,w.definition
                   FROM tasks t JOIN workflow_runs w ON w.id=t.run_id
                   WHERE t.status IN ('queued','retry_wait') AND t.available_at<=clock_timestamp()
                     AND t.last_worker IS DISTINCT FROM %s
                     AND w.status NOT IN ('cancelled','completed','failed')
                   ORDER BY t.available_at,t.created_at
                   FOR UPDATE OF t SKIP LOCKED LIMIT 1"""
        task = conn.execute(selection, (worker_id,)).fetchone()
        if not task:
            # If this worker is the only available capacity, allow it to continue its prior run.
            task = conn.execute(
                selection.replace("AND t.last_worker IS DISTINCT FROM %s", ""), ()
            ).fetchone()
        if not task:
            return None
        conn.execute(
            """UPDATE tasks SET status='running',attempts=attempts+1,lease_owner=%s,
                      lease_until=clock_timestamp()+(%s * interval '1 second'),last_worker=%s,
                      updated_at=clock_timestamp() WHERE id=%s""",
            (worker_id, lease_seconds, worker_id, task["id"]),
        )
        conn.execute(
            "UPDATE workflow_runs SET status='running' WHERE id=%s AND status IN ('queued','retrying')",
            (task["run_id"],),
        )
        emit_event(conn, task["tenant_id"], "task.claimed", run_id=task["run_id"], task_id=task["id"],
                   worker_id=worker_id, request_id=task["request_id"],
                   data={"attempt": task["attempts"] + 1, "step_index": task["step_index"]})
        task["attempts"] += 1
        return task


def heartbeat(conn: Connection, worker_id: str, task_id: str | None, lease_seconds: float) -> None:
    with conn.transaction():
        conn.execute(
            """INSERT INTO workers(id,pid,host) VALUES (%s,%s,%s)
               ON CONFLICT(id) DO UPDATE SET pid=EXCLUDED.pid,host=EXCLUDED.host,
                 heartbeat_at=clock_timestamp(),stopped_at=NULL""",
            (worker_id, os.getpid(), socket.gethostname()),
        )
        if task_id:
            conn.execute(
                """UPDATE tasks SET lease_until=clock_timestamp()+(%s * interval '1 second'),
                          updated_at=clock_timestamp()
                   WHERE id=%s AND status='running' AND lease_owner=%s""",
                (lease_seconds, task_id, worker_id),
            )


def mark_worker_stopped(conn: Connection, worker_id: str) -> None:
    conn.execute("UPDATE workers SET stopped_at=clock_timestamp() WHERE id=%s", (worker_id,))


def recover_expired_leases(conn: Connection, request_id: str = "coordinator") -> int:
    recovered = 0
    with conn.transaction():
        expired = conn.execute(
            """SELECT id,tenant_id,run_id,attempts,max_attempts,last_worker,request_id
               FROM tasks WHERE status='running' AND lease_until<clock_timestamp()
               FOR UPDATE SKIP LOCKED"""
        ).fetchall()
        for task in expired:
            recovered += 1
            if task["attempts"] >= task["max_attempts"]:
                error = {"kind": "lease_expired", "detail": "Worker lease expired; retry budget exhausted."}
                conn.execute(
                    """UPDATE tasks SET status='dead',lease_owner=NULL,lease_until=NULL,last_error=%s,
                       updated_at=clock_timestamp() WHERE id=%s""",
                    (Jsonb(error), task["id"]),
                )
                conn.execute("UPDATE workflow_runs SET status='failed',finished_at=clock_timestamp() WHERE id=%s",
                             (task["run_id"],))
                conn.execute(
                    "INSERT INTO dead_letters(tenant_id,run_id,task_id,attempts,error) VALUES (%s,%s,%s,%s,%s)",
                    (task["tenant_id"], task["run_id"], task["id"], task["attempts"], Jsonb(error)),
                )
                kind = "task.dead_lettered"
            else:
                delay = min(30, 0.25 * (2 ** max(0, task["attempts"] - 1)))
                conn.execute(
                    """UPDATE tasks SET status='retry_wait',available_at=clock_timestamp()+(%s * interval '1 second'),
                              lease_owner=NULL,lease_until=NULL,last_error=%s,updated_at=clock_timestamp()
                       WHERE id=%s""",
                    (delay, Jsonb({"kind": "lease_expired", "detail": "Worker lease expired."}), task["id"]),
                )
                conn.execute("UPDATE workflow_runs SET status='retrying' WHERE id=%s", (task["run_id"],))
                kind = "task.lease_expired"
            emit_event(conn, task["tenant_id"], kind, run_id=task["run_id"], task_id=task["id"],
                       worker_id=task["last_worker"], request_id=task["request_id"],
                       data={"attempts": task["attempts"], "max_attempts": task["max_attempts"]})
    return recovered


def fail_task(conn: Connection, task: dict[str, Any], worker_id: str, error: Exception, request_id: str) -> None:
    data = {"kind": type(error).__name__, "detail": str(error)[:500]}
    with conn.transaction():
        row = conn.execute(
            "SELECT tenant_id,run_id,attempts,max_attempts FROM tasks WHERE id=%s AND lease_owner=%s FOR UPDATE",
            (task["id"], worker_id),
        ).fetchone()
        if not row:
            return
        if row["attempts"] >= row["max_attempts"]:
            conn.execute(
                """UPDATE tasks SET status='dead',lease_owner=NULL,lease_until=NULL,last_error=%s,
                   updated_at=clock_timestamp() WHERE id=%s""",
                (Jsonb(data), task["id"]),
            )
            conn.execute("UPDATE workflow_runs SET status='failed',finished_at=clock_timestamp() WHERE id=%s",
                         (task["run_id"],))
            conn.execute(
                "INSERT INTO dead_letters(tenant_id,run_id,task_id,attempts,error) VALUES (%s,%s,%s,%s,%s)",
                (row["tenant_id"], row["run_id"], task["id"], row["attempts"], Jsonb(data)),
            )
            kind = "task.dead_lettered"
        else:
            delay = min(30, 0.25 * (2 ** max(0, row["attempts"] - 1)))
            conn.execute(
                """UPDATE tasks SET status='retry_wait',available_at=clock_timestamp()+(%s * interval '1 second'),
                          lease_owner=NULL,lease_until=NULL,last_error=%s,updated_at=clock_timestamp()
                   WHERE id=%s""",
                (delay, Jsonb(data), task["id"]),
            )
            conn.execute("UPDATE workflow_runs SET status='retrying' WHERE id=%s", (task["run_id"],))
            kind = "task.retry_scheduled"
        emit_event(conn, row["tenant_id"], kind, run_id=row["run_id"], task_id=task["id"],
                   worker_id=worker_id, request_id=request_id,
                   data={**data, "attempts": row["attempts"], "max_attempts": row["max_attempts"]})


def execute_step(conn: Connection, worker_id: str, task: dict[str, Any], request_id: str) -> None:
    definition = task["definition"]
    steps = definition["steps"]
    index = task["step_index"]
    if index >= len(steps):
        with conn.transaction():
            conn.execute("UPDATE tasks SET status='succeeded',lease_owner=NULL,lease_until=NULL WHERE id=%s",
                         (task["id"],))
            conn.execute("UPDATE workflow_runs SET status='completed',finished_at=clock_timestamp() WHERE id=%s",
                         (task["run_id"],))
            emit_event(conn, task["tenant_id"], "workflow.completed", run_id=task["run_id"],
                       task_id=task["id"], worker_id=worker_id, request_id=request_id)
        return

    step = steps[index]
    action, payload = step["action"], step["payload"]
    if action == "fail_once" and task["attempts"] == 1:
        raise RuntimeError("Deterministic Demo Mode failure on the first attempt.")
    if action == "fail_until_replay":
        released = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM dead_letters WHERE task_id=%s AND replayed_at IS NOT NULL) AS released",
            (task["id"],),
        ).fetchone()["released"]
        if not released:
            raise RuntimeError("Demo failure is held until an administrator replays its dead letter.")

    if action == "sleep":
        from app.settings import LEASE_SECONDS

        remaining = float(payload.get("seconds", 0))
        tick = min(0.2, max(0.05, LEASE_SECONDS / 4))
        while remaining > 0:
            duration = min(tick, remaining)
            time.sleep(duration)
            remaining -= duration
            heartbeat(conn, worker_id, task["id"], LEASE_SECONDS)
            cancelled = conn.execute("SELECT status FROM workflow_runs WHERE id=%s", (task["run_id"],)).fetchone()
            if cancelled and cancelled["status"] == "cancelled":
                with conn.transaction():
                    conn.execute("UPDATE tasks SET status='cancelled',lease_owner=NULL,lease_until=NULL WHERE id=%s",
                                 (task["id"],))
                return

    result = {"action": action, **payload}
    with conn.transaction():
        locked = conn.execute(
            """SELECT t.tenant_id,t.run_id,t.step_index,w.definition,w.status
               FROM tasks t JOIN workflow_runs w ON w.id=t.run_id
               WHERE t.id=%s AND t.lease_owner=%s AND t.lease_until>clock_timestamp()
                 AND t.status='running' FOR UPDATE OF t,w""",
            (task["id"], worker_id),
        ).fetchone()
        if not locked or locked["status"] == "cancelled":
            return
        if locked["step_index"] != index:
            return
        effect_key = f"{task['run_id']}:{index}"
        created = conn.execute(
            """INSERT INTO side_effects(id,tenant_id,run_id,idempotency_key,step_index,result)
               VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,idempotency_key) DO NOTHING
               RETURNING id""",
            (str(uuid.uuid4()), task["tenant_id"], task["run_id"], effect_key, index, Jsonb(result)),
        ).fetchone()
        emit_event(conn, task["tenant_id"], "side_effect.applied" if created else "side_effect.deduplicated",
                   run_id=task["run_id"], task_id=task["id"], worker_id=worker_id, request_id=request_id,
                   data={"step_index": index, "idempotency_key": effect_key})
        next_index = index + 1
        if next_index == len(locked["definition"]["steps"]):
            conn.execute(
                """UPDATE tasks SET status='succeeded',step_index=%s,attempts=0,lease_owner=NULL,
                          lease_until=NULL,updated_at=clock_timestamp() WHERE id=%s""",
                (next_index, task["id"]),
            )
            conn.execute("UPDATE workflow_runs SET status='completed',finished_at=clock_timestamp() WHERE id=%s",
                         (task["run_id"],))
            kind = "workflow.completed"
        else:
            conn.execute(
                """UPDATE tasks SET status='queued',step_index=%s,attempts=0,lease_owner=NULL,
                          lease_until=NULL,available_at=clock_timestamp(),updated_at=clock_timestamp()
                   WHERE id=%s""",
                (next_index, task["id"]),
            )
            kind = "task.step_completed"
        emit_event(conn, task["tenant_id"], kind, run_id=task["run_id"], task_id=task["id"],
                   worker_id=worker_id, request_id=request_id,
                   data={"step_index": index, "step_name": step["name"]})


def run_summary(conn: Connection, tenant_id: str, run_id: str) -> dict[str, Any] | None:
    run = conn.execute(
        """SELECT w.id,w.title,w.status,w.definition,w.created_at,w.finished_at,
                  t.id AS task_id,t.status AS task_status,t.step_index,t.attempts,t.max_attempts,
                  t.lease_owner,t.lease_until,t.last_error
           FROM workflow_runs w JOIN tasks t ON t.run_id=w.id
           WHERE w.tenant_id=%s AND w.id=%s""",
        (tenant_id, run_id),
    ).fetchone()
    if not run:
        return None
    run["events"] = conn.execute(
        "SELECT sequence,kind,worker_id,request_id,data,created_at FROM events WHERE tenant_id=%s AND run_id=%s ORDER BY sequence",
        (tenant_id, run_id),
    ).fetchall()
    run["side_effects"] = conn.execute(
        "SELECT idempotency_key,step_index,result,created_at FROM side_effects WHERE tenant_id=%s AND run_id=%s ORDER BY step_index",
        (tenant_id, run_id),
    ).fetchall()
    return run


def dashboard(conn: Connection, tenant_id: str) -> dict[str, Any]:
    counts = conn.execute(
        """SELECT count(*) FILTER (WHERE status IN ('queued','retry_wait')) AS queue_depth,
                  count(*) FILTER (WHERE status='running') AS running,
                  count(*) FILTER (WHERE status='completed') AS completed,
                  count(*) FILTER (WHERE status='failed') AS failed
           FROM workflow_runs WHERE tenant_id=%s""", (tenant_id,)
    ).fetchone()
    queue = conn.execute(
        "SELECT count(*) AS count FROM tasks WHERE tenant_id=%s AND status IN ('queued','retry_wait','running')",
        (tenant_id,),
    ).fetchone()["count"]
    events = conn.execute(
        """SELECT count(*) AS received_count,coalesce(sum(received_count-1),0) AS duplicate_count
           FROM business_events WHERE tenant_id=%s""", (tenant_id,)
    ).fetchone()
    effects = conn.execute("SELECT count(*) AS count FROM side_effects WHERE tenant_id=%s", (tenant_id,)).fetchone()["count"]
    dlq = conn.execute("SELECT count(*) AS count FROM dead_letters WHERE tenant_id=%s AND replayed_at IS NULL", (tenant_id,)).fetchone()["count"]
    workers = conn.execute(
        """SELECT id,pid,host,started_at,heartbeat_at,stopped_at,
                  heartbeat_at>clock_timestamp()-interval '3 seconds' AND stopped_at IS NULL AS alive
           FROM workers ORDER BY id"""
    ).fetchall()
    workflows = conn.execute(
        """SELECT w.id,w.title,w.status,w.created_at,t.step_index,t.attempts,t.last_worker,t.lease_owner,t.last_error
           FROM workflow_runs w JOIN tasks t ON t.run_id=w.id
           WHERE w.tenant_id=%s ORDER BY w.created_at DESC LIMIT 30""", (tenant_id,)
    ).fetchall()
    latencies = conn.execute(
        """SELECT percentile_cont(0.50) WITHIN GROUP (ORDER BY extract(epoch FROM (finished_at-created_at))*1000) AS p50,
                  percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM (finished_at-created_at))*1000) AS p95,
                  percentile_cont(0.99) WITHIN GROUP (ORDER BY extract(epoch FROM (finished_at-created_at))*1000) AS p99
           FROM workflow_runs WHERE tenant_id=%s AND finished_at IS NOT NULL""", (tenant_id,)
    ).fetchone()
    return {"counts": counts, "queue_depth": queue, "events": events, "side_effect_count": effects,
            "dlq_size": dlq, "workers": workers, "workflows": workflows, "latency_ms": latencies}

