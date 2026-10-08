from __future__ import annotations

import os
from typing import Any, TypeAlias

import psycopg
from cryptography.fernet import Fernet
from psycopg import Connection
from psycopg.rows import dict_row

from app.settings import DEMO_MODE, validate_database_tls
from app.secretbox import credential_fingerprint, decrypt_secret, encrypt_secret

DatabaseConnection: TypeAlias = Connection[dict[str, Any]]


def _key(name: str) -> bytes:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Set {name} before rotating stored secrets.")
    try:
        Fernet(value.encode())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be a valid Fernet key.") from exc
    return value.encode()


def rotate_secrets(conn: DatabaseConnection, old_key: bytes, new_key: bytes) -> dict[str, int]:
    if old_key == new_key:
        raise ValueError("The replacement encryption key must differ from the current key.")

    counts = {"github_oauth_states": 0, "webhook_secrets": 0, "integration_credential_secrets": 0}
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('relaycore:secret-key-rotation', 0))")
        conn.execute(
            "LOCK TABLE github_oauth_states, webhook_secrets, integration_credentials, "
            "integration_credential_secrets IN SHARE ROW EXCLUSIVE MODE"
        )
        row = conn.execute(
            """SELECT count(*) AS count FROM integration_credentials c
               WHERE NOT EXISTS (
                   SELECT 1 FROM integration_credential_secrets s
                   WHERE s.credential_id=c.id AND s.version=1
               )"""
        ).fetchone()
        assert row is not None
        missing_creation_secrets = row["count"]
        if missing_creation_secrets:
            raise RuntimeError("Cannot rotate credentials without their original secret versions.")

        with conn.cursor(name="relaycore_rotate_webhook_secrets") as rows:
            rows.itersize = 250
            rows.execute(
                "SELECT endpoint_id,version,encrypted_secret FROM webhook_secrets ORDER BY endpoint_id,version"
            )
            for row in rows:
                conn.execute(
                    "UPDATE webhook_secrets SET encrypted_secret=%s WHERE endpoint_id=%s AND version=%s",
                    (encrypt_secret(decrypt_secret(row["encrypted_secret"], old_key), new_key),
                     row["endpoint_id"], row["version"]),
                )
                counts["webhook_secrets"] += 1

        with conn.cursor(name="relaycore_rotate_credential_secrets") as rows:
            rows.itersize = 250
            rows.execute(
                """SELECT c.id,c.provider,c.name,c.allowed_host,s.version,s.encrypted_secret
                   FROM integration_credentials c
                   JOIN integration_credential_secrets s ON s.credential_id=c.id
                   ORDER BY c.id,s.version"""
            )
            for row in rows:
                secret = decrypt_secret(row["encrypted_secret"], old_key)
                fingerprint = credential_fingerprint(
                    new_key, row["provider"], row["name"], row["allowed_host"], secret,
                )
                conn.execute(
                    """UPDATE integration_credential_secrets
                       SET encrypted_secret=%s,secret_fingerprint=%s WHERE credential_id=%s AND version=%s""",
                    (encrypt_secret(secret, new_key), fingerprint, row["id"], row["version"]),
                )
                if row["version"] == 1:
                    conn.execute(
                        "UPDATE integration_credentials SET creation_fingerprint=%s WHERE id=%s",
                        (fingerprint, row["id"]),
                    )
                counts["integration_credential_secrets"] += 1

        with conn.cursor(name="relaycore_rotate_github_oauth_states") as rows:
            rows.itersize = 250
            rows.execute("SELECT state_hash,verifier_encrypted FROM github_oauth_states ORDER BY state_hash")
            for row in rows:
                conn.execute(
                    "UPDATE github_oauth_states SET verifier_encrypted=%s WHERE state_hash=%s",
                    (encrypt_secret(decrypt_secret(row["verifier_encrypted"], old_key), new_key), row["state_hash"]),
                )
                counts["github_oauth_states"] += 1

    return counts


def main() -> None:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("Set DATABASE_URL to the RelayCore database before rotating stored secrets.")
    validate_database_tls(database_url, demo_mode=DEMO_MODE)
    old_key = _key("RELAYCORE_SECRET_ENCRYPTION_KEY")
    new_key = _key("RELAYCORE_SECRET_ENCRYPTION_NEW_KEY")
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        counts = rotate_secrets(conn, old_key, new_key)
    print("Secret encryption key rotation completed in one transaction: " + ", ".join(
        f"{table}={count}" for table, count in counts.items()
    ))


if __name__ == "__main__":
    main()
