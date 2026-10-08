from __future__ import annotations

import time
from typing import Any

from psycopg import Connection

from app.http_action import PermanentActionError
from app.settings import DEMO_MODE, LEASE_SECONDS


def execute_sandbox_action(
    conn: Connection[dict[str, Any]], worker_id: str, task: dict[str, Any], step: dict[str, Any],
) -> dict[str, Any] | None:
    if not DEMO_MODE:
        raise PermanentActionError("Sandbox actions are disabled outside Demo Mode.")

    action, payload = step["action"], step["payload"]
    if action == "fail_once" and task["attempts"] == 1:
        raise RuntimeError("Deterministic Demo Mode failure on the first attempt.")
    if action == "fail_until_replay":
        released_row = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM dead_letters WHERE task_id=%s AND replayed_at IS NOT NULL) AS released",
            (task["id"],),
        ).fetchone()
        assert released_row is not None
        released = released_row["released"]
        if not released:
            raise RuntimeError("Demo failure is held until an administrator replays its dead letter.")
    if action == "sleep":
        from app.store import heartbeat

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
                return None
    if action not in {"record", "charge", "sleep", "fail_once", "fail_until_replay"}:
        raise PermanentActionError("Unknown sandbox action.")
    return {"action": action, **payload}
