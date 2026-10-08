from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.store import create_workflow, migrate

ROOT = Path(__file__).resolve().parents[1]
DATABASE_NAME = re.compile(r"^relaycore_restore_[a-z0-9_]+$")


def _database_name(variable: str) -> tuple[str, tuple[str, int | None]]:
    parsed = urlsplit(os.environ[variable])
    name = parsed.path.lstrip("/")
    if parsed.hostname not in {"127.0.0.1", "localhost"} or not DATABASE_NAME.fullmatch(name):
        raise RuntimeError(f"{variable} must target a localhost database named relaycore_restore_*.")
    return name, (parsed.hostname, parsed.port)


def main() -> None:
    if os.environ.get("RELAYCORE_RESTORE_DRILL") != "1":
        raise RuntimeError("Set RELAYCORE_RESTORE_DRILL=1 to create disposable restore-drill databases.")
    admin_url = os.environ["RELAYCORE_RESTORE_ADMIN_URL"]
    admin = urlsplit(admin_url)
    if admin.hostname not in {"127.0.0.1", "localhost"} or admin.path.strip("/") != "postgres":
        raise RuntimeError("RELAYCORE_RESTORE_ADMIN_URL must connect to the local postgres maintenance database.")

    source_name, source_host = _database_name("DATABASE_URL")
    target_name, target_host = _database_name("RELAYCORE_RESTORE_DATABASE_URL")
    if source_name == target_name or source_host != target_host or source_host != (admin.hostname, admin.port):
        raise RuntimeError("Restore-drill URLs must use distinct databases on the same localhost PostgreSQL server.")

    with psycopg.connect(admin_url, autocommit=True) as conn:
        existing = conn.execute(
            "SELECT datname FROM pg_database WHERE datname=ANY(%s)", ([source_name, target_name],),
        ).fetchall()
        if existing:
            raise RuntimeError("Restore-drill databases already exist; use a fresh disposable PostgreSQL instance.")
        for name in (source_name, target_name):
            conn.execute(sql.SQL("CREATE DATABASE {} WITH ENCODING 'UTF8' TEMPLATE template0").format(
                sql.Identifier(name),
            ))

    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        migrate(conn)
        expected_migrations = len(list((ROOT / "app" / "migrations").glob("[0-9]*_*.sql")))
        source_migrations = conn.execute("SELECT count(*) AS count FROM schema_migrations").fetchone()["count"]
        if source_migrations != expected_migrations:
            raise AssertionError(f"Expected {expected_migrations} source migrations, found {source_migrations}.")
        run = create_workflow(
            conn, "restore-drill-workspace", "restore-drill-marker",
            [{"name": "marker", "action": "record", "payload": {"marker": "restored"}}],
            "restore-drill:known-record", "restore-drill-request",
        )
        if not run["created"]:
            raise AssertionError("Source marker workflow was not created.")

    shell = shutil.which("pwsh")
    if not shell:
        raise RuntimeError("PowerShell Core is required to run the backup and restore scripts.")

    with tempfile.TemporaryDirectory(prefix="relaycore-restore-drill-") as directory:
        archive = Path(directory) / "relaycore.dump"
        if archive.is_relative_to(ROOT):
            raise RuntimeError("Restore-drill archive must remain outside the repository.")
        subprocess.run(
            [shell, "-NoProfile", "-File", str(ROOT / "scripts" / "backup.ps1"),
             "-Destination", str(archive)],
            check=True,
        )
        if not archive.is_file() or archive.stat().st_size == 0:
            raise AssertionError("Backup script did not produce a non-empty archive.")
        subprocess.run(
            [shell, "-NoProfile", "-File", str(ROOT / "scripts" / "restore.ps1"),
             "-BackupPath", str(archive), "-TargetDatabaseUrl",
             os.environ["RELAYCORE_RESTORE_DATABASE_URL"], "-Confirm:$false"],
            check=True,
        )

    with psycopg.connect(os.environ["RELAYCORE_RESTORE_DATABASE_URL"], row_factory=dict_row) as conn:
        migrations = conn.execute("SELECT count(*) AS count FROM schema_migrations").fetchone()["count"]
        marker = conn.execute(
            "SELECT status,definition FROM workflow_runs WHERE tenant_id=%s AND idempotency_key=%s",
            ("restore-drill-workspace", "restore-drill:known-record"),
        ).fetchone()
        if migrations != expected_migrations or not marker or marker["status"] != "queued":
            raise AssertionError("Restored schema or workflow marker did not match the source database.")
        if marker["definition"]["title"] != "restore-drill-marker":
            raise AssertionError("Restored workflow definition did not match the source database.")
    print(f"Backup restore drill passed: {migrations} migrations and workflow marker restored.")


if __name__ == "__main__":
    main()
