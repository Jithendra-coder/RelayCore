from __future__ import annotations

import logging
import threading

import psycopg
from psycopg.rows import dict_row

from app.settings import DATABASE_URL
from app.store import recover_expired_leases

logger = logging.getLogger("relaycore.coordinator")


def run(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            with psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True,
                                 application_name="relaycore-coordinator") as conn:
                count = recover_expired_leases(conn)
            if count:
                logger.info('{"event":"leases.recovered","count":%d}', count)
        except Exception:
            logger.exception("lease recovery pass failed")
        stop.wait(0.25)
