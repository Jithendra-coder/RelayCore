from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time

import psycopg
from psycopg.rows import dict_row

from app.settings import DATABASE_URL, LEASE_SECONDS
from app.store import claim_task, execute_step, fail_task, heartbeat, mark_worker_stopped

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")
logger = logging.getLogger("relaycore.worker")


def main() -> None:
    worker_id = os.environ.get("RELAYCORE_WORKER_ID", f"worker-{os.getpid()}")
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
                        try:
                            execute_step(conn, worker_id, task, task["request_id"])
                        except psycopg.Error:
                            raise
                        except Exception as exc:
                            fail_task(conn, task, worker_id, exc, task["request_id"])
                            logger.info('{"event":"task.failed_or_retried","worker_id":"%s","task_id":"%s"}',
                                        worker_id, task["id"])
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


if __name__ == "__main__":
    main()
