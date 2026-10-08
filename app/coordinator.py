from __future__ import annotations

import logging
import threading
import time

import psycopg
from psycopg.rows import dict_row

from app.settings import DATABASE_URL
from app.store import dispatch_due_schedules, expire_webhook_payloads, recover_expired_leases

logger = logging.getLogger("relaycore.coordinator")


def run(stop: threading.Event) -> None:
    next_schedule_poll = 0.0
    next_payload_sweep = 0.0
    while not stop.is_set():
        expired_payloads = 0
        try:
            with psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True,
                                 application_name="relaycore-coordinator") as conn:
                count = recover_expired_leases(conn)
                schedules = 0
                if time.monotonic() >= next_schedule_poll:
                    schedules = dispatch_due_schedules(conn)
                    next_schedule_poll = time.monotonic() + 1
                if time.monotonic() >= next_payload_sweep:
                    expired_payloads = expire_webhook_payloads(conn)
                    next_payload_sweep = time.monotonic() + (1 if expired_payloads == 500 else 300)
            if count:
                logger.info('{"event":"leases.recovered","count":%d}', count)
            if schedules:
                logger.info('{"event":"schedules.dispatched","count":%d}', schedules)
            if expired_payloads:
                logger.info('{"event":"webhook.payloads_expired","count":%d}', expired_payloads)
        except Exception:
            logger.exception("coordinator pass failed")
        stop.wait(0.25)
