from __future__ import annotations

import logging
import threading
import time

import psycopg
from psycopg.rows import dict_row

from app.settings import DATABASE_URL
from app.store import dispatch_due_schedules, recover_expired_leases

logger = logging.getLogger("relaycore.coordinator")


def run(stop: threading.Event) -> None:
    next_schedule_poll = 0.0
    while not stop.is_set():
        try:
            with psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True,
                                 application_name="relaycore-coordinator") as conn:
                count = recover_expired_leases(conn)
                schedules = 0
                if time.monotonic() >= next_schedule_poll:
                    schedules = dispatch_due_schedules(conn)
                    next_schedule_poll = time.monotonic() + 1
            if count:
                logger.info('{"event":"leases.recovered","count":%d}', count)
            if schedules:
                logger.info('{"event":"schedules.dispatched","count":%d}', schedules)
        except Exception:
            logger.exception("lease recovery pass failed")
        stop.wait(0.25)
