from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager
from typing import Iterator

import pytest
from cryptography.fernet import Fernet
from psycopg import connect, sql
from psycopg.rows import dict_row

from app.settings import DATABASE_URL
from app.secretbox import decrypt_secret, encrypt_secret
from app.store import migrate, upsert_oidc_user
from scripts.rotate_secrets import _key, main, rotate_secrets


def test_rotation_refuses_identical_keys():
    key = Fernet.generate_key()
    with pytest.raises(ValueError, match="must differ"):
        rotate_secrets(None, key, key)


def test_rotation_command_requires_database_and_valid_keys(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        main()

    monkeypatch.setenv("RELAYCORE_SECRET_ENCRYPTION_KEY", "invalid")
    with pytest.raises(RuntimeError, match="valid Fernet key"):
        _key("RELAYCORE_SECRET_ENCRYPTION_KEY")
    key = Fernet.generate_key()
    monkeypatch.setenv("RELAYCORE_SECRET_ENCRYPTION_KEY", key.decode())
    assert _key("RELAYCORE_SECRET_ENCRYPTION_KEY") == key


@contextmanager
def _isolated_schema() -> Iterator:
    schema = f"secret_rotation_{uuid.uuid4().hex}"
    with connect(DATABASE_URL, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        try:
            migrate(conn)
            yield conn
        finally:
            conn.execute("SET search_path TO public")
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _seed_rotation_records(conn, key: bytes) -> dict[str, str]:
    from app.store import (
        create_github_oauth_state,
        create_webhook_endpoint,
        create_workspace,
        create_workspace_credential,
        rotate_workspace_credential,
    )

    suffix = uuid.uuid4().hex
    email = f"rotation-{suffix}@example.test"
    with conn.transaction():
        user = upsert_oidc_user(conn, "https://identity.example", suffix, email, email)
        workspace = create_workspace(conn, user["id"], "rotation test", f"rotation:{suffix}")
        webhook = create_webhook_endpoint(
            conn, workspace["id"], user["id"], "rotation hook", f"rotation:webhook:{suffix}",
            f"rotation:{suffix}", key,
        )
        credential = create_workspace_credential(
            conn, workspace["id"], user["id"], "http", "rotation target", "token-v1-" + suffix,
            "hooks.example.test", f"rotation:credential:{suffix}", f"rotation:{suffix}", key,
        )
        rotated = rotate_workspace_credential(
            conn, workspace["id"], credential["id"], user["id"], "token-v2-" + suffix,
            f"rotation:credential-version:{suffix}", f"rotation:{suffix}", key,
        )
        state_hash = hashlib.sha256(f"state-{suffix}".encode()).hexdigest()
        create_github_oauth_state(
            conn, workspace["id"], user["id"], state_hash, encrypt_secret("pkce-verifier-" + suffix, key),
        )

    assert rotated is not None
    return {
        "user_id": user["id"],
        "workspace_id": workspace["id"],
        "webhook_id": webhook["id"],
        "webhook_secret": webhook["secret"],
        "webhook_key": f"rotation:webhook:{suffix}",
        "credential_id": credential["id"],
        "credential_key": f"rotation:credential:{suffix}",
        "credential_secret": "token-v1-" + suffix,
        "credential_rotation_key": f"rotation:credential-version:{suffix}",
        "credential_rotated_secret": "token-v2-" + suffix,
        "state_hash": state_hash,
        "verifier": "pkce-verifier-" + suffix,
        "suffix": suffix,
    }


def _ciphertexts(conn, records: dict[str, str]) -> tuple[str, list[str], str]:
    oauth = conn.execute(
        "SELECT verifier_encrypted FROM github_oauth_states WHERE state_hash=%s",
        (records["state_hash"],),
    ).fetchone()["verifier_encrypted"]
    webhook = conn.execute(
        "SELECT encrypted_secret FROM webhook_secrets WHERE endpoint_id=%s ORDER BY version",
        (records["webhook_id"],),
    ).fetchall()
    credentials = conn.execute(
        "SELECT encrypted_secret FROM integration_credential_secrets WHERE credential_id=%s ORDER BY version",
        (records["credential_id"],),
    ).fetchall()
    return oauth, [row["encrypted_secret"] for row in webhook], credentials[0]["encrypted_secret"]


def test_rotation_rewraps_all_secret_rows_and_preserves_idempotency():
    from app.secretbox import credential_fingerprint
    from app.store import (
        create_webhook_endpoint,
        create_workspace_credential,
        rotate_workspace_credential,
        webhook_signing_info,
        workspace_credential_secret,
    )

    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()
    with _isolated_schema() as conn:
        records = _seed_rotation_records(conn, old_key)
        before = _ciphertexts(conn, records)
        counts = rotate_secrets(conn, old_key, new_key)
        assert counts == {
            "github_oauth_states": 1,
            "webhook_secrets": 1,
            "integration_credential_secrets": 2,
        }
        after = _ciphertexts(conn, records)
        assert after[0] != before[0]
        assert after[1] != before[1]
        assert after[2] != before[2]

        assert decrypt_secret(after[0], new_key) == records["verifier"]
        webhook = webhook_signing_info(conn, records["webhook_id"])
        assert decrypt_secret(webhook["encrypted_secret"], new_key) == records["webhook_secret"]
        credential = workspace_credential_secret(conn, records["workspace_id"], records["credential_id"], new_key)
        assert credential["secret"] == records["credential_rotated_secret"]

        created_retry = create_workspace_credential(
            conn, records["workspace_id"], records["user_id"], "http", "rotation target",
            records["credential_secret"], "hooks.example.test", records["credential_key"],
            f"rotation:{records['suffix']}:retry", new_key,
        )
        rotated_retry = rotate_workspace_credential(
            conn, records["workspace_id"], records["credential_id"], records["user_id"],
            records["credential_rotated_secret"], records["credential_rotation_key"],
            f"rotation:{records['suffix']}:retry", new_key,
        )
        webhook_retry = create_webhook_endpoint(
            conn, records["workspace_id"], records["user_id"], "rotation hook", records["webhook_key"],
            f"rotation:{records['suffix']}:retry", new_key,
        )
        stored = conn.execute(
            "SELECT creation_fingerprint FROM integration_credentials WHERE id=%s",
            (records["credential_id"],),
        ).fetchone()["creation_fingerprint"]

    assert created_retry["created"] is False
    assert rotated_retry and rotated_retry["created"] is False
    assert webhook_retry["created"] is False and webhook_retry["secret"] == records["webhook_secret"]
    assert stored == credential_fingerprint(
        new_key, "http", "rotation target", "hooks.example.test", records["credential_secret"],
    )


def test_rotation_rolls_back_every_table_when_a_ciphertext_is_corrupt():
    from app.secretbox import SecretStorageError

    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()
    with _isolated_schema() as conn:
        records = _seed_rotation_records(conn, old_key)
        before = _ciphertexts(conn, records)
        with conn.transaction():
            conn.execute(
                "UPDATE github_oauth_states SET verifier_encrypted='invalid' WHERE state_hash=%s",
                (records["state_hash"],),
            )

        with pytest.raises(SecretStorageError):
            rotate_secrets(conn, old_key, new_key)

        after = _ciphertexts(conn, records)
        assert after[0] == "invalid"
        assert after[1:] == before[1:]
