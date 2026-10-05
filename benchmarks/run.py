from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.coordinator import run as run_coordinator  # noqa: E402
from app.settings import DATABASE_URL, LEASE_SECONDS, MAX_ATTEMPTS  # noqa: E402
from app.store import create_workflow, migrate  # noqa: E402
from app.supervisor import WorkerSupervisor  # noqa: E402


def percentile(samples: list[float], p: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * p))]


def queue_plan(dsn: str, rows: int = 20000) -> dict:
    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as conn:
        conn.execute("""CREATE TEMP TABLE relaycore_plan_tasks (
            id bigint,tenant_id text,status text,available_at timestamptz,created_at timestamptz)""")
        conn.execute(
            """INSERT INTO relaycore_plan_tasks
               SELECT n,'plan-tenant','queued',clock_timestamp()-(n*interval '1 millisecond'),
                      clock_timestamp()-(n*interval '1 millisecond')
               FROM generate_series(1,%s) AS n""", (rows,)
        )
        conn.execute("ANALYZE relaycore_plan_tasks")
        query = """EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON)
                   SELECT id FROM relaycore_plan_tasks
                   WHERE status IN ('queued','retry_wait')
                     AND available_at<=clock_timestamp()
                   ORDER BY available_at,created_at LIMIT 1"""
        before = conn.execute(query).fetchone()["QUERY PLAN"][0]
        conn.execute("""CREATE INDEX relaycore_plan_ready_idx
                       ON relaycore_plan_tasks(available_at,created_at)
                       WHERE status IN ('queued','retry_wait')""")
        conn.execute("ANALYZE relaycore_plan_tasks")
        after = conn.execute(query).fetchone()["QUERY PLAN"][0]
        return {"fixture_rows": rows, "before_index": before, "after_index": after}


def load_scenario(pool: ConnectionPool, workers: int, count: int) -> dict:
    tenant = f"bench-{workers}-{uuid.uuid4().hex[:8]}"
    supervisor = WorkerSupervisor(workers)
    stop = threading.Event()
    coordinator = threading.Thread(target=run_coordinator, args=(stop,), daemon=True)
    supervisor.start()
    coordinator.start()
    started = time.perf_counter()
    sample_ms = []
    peak_queue = 0
    try:
        with pool.connection() as conn:
            for index in range(count):
                create_workflow(
                    conn,
                    tenant,
                    f"benchmark-{index}",
                    [
                        {"name": "reserve", "action": "record", "payload": {"index": index}},
                        {"name": "finish", "action": "record", "payload": {"index": index}},
                    ],
                    f"bench:{workers}:{index}",
                    "benchmark-run",
                )
        while time.perf_counter() - started < 120:
            with pool.connection() as conn:
                state = conn.execute(
                    """SELECT status,count(*) AS n FROM workflow_runs WHERE tenant_id=%s GROUP BY status""",
                    (tenant,),
                ).fetchall()
                counts = {row["status"]: row["n"] for row in state}
                peak_queue = max(peak_queue, conn.execute(
                    "SELECT count(*) AS n FROM tasks WHERE tenant_id=%s AND status IN ('queued','retry_wait','running')",
                    (tenant,),
                ).fetchone()["n"])
                finished = conn.execute(
                    """SELECT extract(epoch FROM (finished_at-created_at))*1000 AS latency_ms
                       FROM workflow_runs WHERE tenant_id=%s AND finished_at IS NOT NULL""", (tenant,)
                ).fetchall()
                sample_ms = [float(row["latency_ms"]) for row in finished]
                done = counts.get("completed", 0) + counts.get("failed", 0)
            if done >= count:
                break
            time.sleep(0.05)
        elapsed = time.perf_counter() - started
        with pool.connection() as conn:
            failed = conn.execute("SELECT count(*) AS n FROM workflow_runs WHERE tenant_id=%s AND status='failed'",
                                  (tenant,)).fetchone()["n"]
        completed = len(sample_ms)
        if completed + failed < count:
            raise TimeoutError(f"Only {completed + failed}/{count} benchmark workflows finished.")
        return {
            "classification": "MEASURED",
            "workers": workers,
            "workflows_submitted": count,
            "workflows_completed": completed,
            "workflows_failed": failed,
            "workflow_error_rate": failed / count,
            "workflows_per_second": round(completed / elapsed, 2),
            "steps_per_second": round((2 * completed) / elapsed, 2),
            "workflow_latency_ms": {"p50": round(percentile(sample_ms, .50), 2),
                                    "p95": round(percentile(sample_ms, .95), 2),
                                    "p99": round(percentile(sample_ms, .99), 2)},
            "peak_active_queue_depth": peak_queue,
            "elapsed_seconds": round(elapsed, 3),
        }
    finally:
        stop.set()
        coordinator.join(timeout=2)
        supervisor.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure RelayCore against a PostgreSQL database.")
    parser.add_argument("--workflows", type=int, default=100, help="synthetic two-step runs per worker-count scenario")
    parser.add_argument("--output", type=Path, default=ROOT / "benchmarks" / "results" / "local-run.json")
    args = parser.parse_args()
    if not 10 <= args.workflows <= 300:
        parser.error("--workflows must be between 10 and 300 (respecting the per-tenant demo rate limit).")
    dsn = os.environ.get("DATABASE_URL", DATABASE_URL)
    pool = ConnectionPool(dsn, min_size=1, max_size=12, kwargs={"row_factory": dict_row}, open=False)
    pool.open(wait=True)
    try:
        with pool.connection() as conn:
            migrate(conn)
            server = conn.execute("SELECT version() AS version, current_setting('server_encoding') AS encoding").fetchone()
        if server["encoding"] != "UTF8":
            raise RuntimeError("RelayCore requires a UTF8 PostgreSQL database.")
        scenarios = [load_scenario(pool, workers, args.workflows) for workers in (1, 2, 4)]
        report = {
            "classification": "MEASURED",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "environment": {"python": sys.version.split()[0], "platform": platform.platform(),
                            "logical_cpu_count": os.cpu_count(), "postgresql": server["version"],
                            "server_encoding": server["encoding"], "lease_seconds": LEASE_SECONDS,
                            "max_attempts": MAX_ATTEMPTS,
                            "workload": f"{args.workflows} synthetic two-step workflows for each worker count; record-only demo effects"},
            "worker_scaling": scenarios,
            "database_query_plan": queue_plan(dsn),
            "limitations": [
                "Synthetic local workload and one PostgreSQL instance; results do not predict cloud or external-service performance.",
                "Workflow side effects are local database records and intentionally have no external network latency.",
            ],
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps({"output": str(args.output), "worker_scaling": scenarios,
                          "database_plan_ms": {"before": report["database_query_plan"]["before_index"]["Execution Time"],
                                               "after": report["database_query_plan"]["after_index"]["Execution Time"]}}, indent=2))
    finally:
        pool.close()


if __name__ == "__main__":
    main()
