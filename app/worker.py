from __future__ import annotations

import logging
import json
import os
import re
import signal
import socket
import threading

import psycopg
from opentelemetry import trace
from psycopg.rows import dict_row

from app.settings import DATABASE_URL, LEASE_SECONDS
from app.store import claim_task, execute_step, fail_task, heartbeat, mark_worker_stopped
from app.telemetry import configure_tracing, mark_span_failed, workflow_step_span

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")
logger = logging.getLogger("relaycore.worker")


def _log_task_failure(worker_id: str, task: dict, error: Exception) -> None:
    logger.warning(json.dumps({
        "event": "task.failed_or_retried",
        "worker_id": worker_id,
        "request_id": task["request_id"],
        "run_id": task["run_id"],
        "task_id": task["id"],
        "attempt": task["attempts"],
        "step_index": task["step_index"],
        "error_type": type(error).__name__,
    }, separators=(",", ":")))


def worker_identifier() -> str:
    worker_id = os.environ.get("RELAYCORE_WORKER_ID") or f"worker-{socket.gethostname()}-{os.getpid()}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", worker_id):
        raise RuntimeError("RELAYCORE_WORKER_ID must be 1–128 safe identifier characters.")
    return worker_id


def worker_is_healthy() -> bool:
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=2, autocommit=True) as conn:
            return conn.execute(
                """SELECT EXISTS (
                       SELECT 1 FROM workers
                       WHERE host=%s AND stopped_at IS NULL
                         AND heartbeat_at>clock_timestamp()-interval '15 seconds'
                   )""",
                (socket.gethostname(),),
            ).fetchone()[0]
    except psycopg.Error:
        return False


def main() -> None:
    worker_id = worker_identifier()
    tracer_provider = configure_tracing("relaycore-worker")
    tracer = trace.get_tracer("relaycore.worker")
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: stopped.set())

    logger.info('{"event":"worker.started","worker_id":"%s","pid":%s,"host":"%s"}',
                worker_id, os.getpid(), socket.gethostname())
    while not stopped.is_set():
        try:
            with psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True,
                                 application_name=f"relaycore-{worker_id}") as conn:
                while not stopped.is_set():
                    try:
                        heartbeat(conn, worker_id, None, LEASE_SECONDS)
                        task = claim_task(conn, worker_id, LEASE_SECONDS)
                        if task is None:
                            stopped.wait(0.35)
                            continue
                        with workflow_step_span(tracer, task, worker_id) as span:
                            try:
                                execute_step(conn, worker_id, task, task["request_id"])
                            except psycopg.Error as exc:
                                mark_span_failed(span, exc)
                                raise
                            except Exception as exc:
                                mark_span_failed(span, exc)
                                fail_task(conn, task, worker_id, exc, task["request_id"])
                                _log_task_failure(worker_id, task, exc)
                    except psycopg.Error:
                        logger.exception('{"event":"worker.database_connection_lost","worker_id":"%s"}', worker_id)
                        break
                if stopped.is_set():
                    mark_worker_stopped(conn, worker_id)
        except psycopg.Error:
            logger.exception('{"event":"worker.database_unavailable","worker_id":"%s"}', worker_id)
        if not stopped.is_set():
            stopped.wait(0.5)
    logger.info('{"event":"worker.stopped","worker_id":"%s"}', worker_id)
    if tracer_provider:
        tracer_provider.shutdown()


if __name__ == "__main__":
    main()
