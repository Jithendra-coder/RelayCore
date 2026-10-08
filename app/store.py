from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import uuid
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from app.secretbox import credential_fingerprint, decrypt_secret, encrypt_secret
from app.http_action import PermanentActionError, RetryableActionError, has_event_references
from app.telemetry import current_traceparent
from app.settings import (DEMO_MODE, MAX_ATTEMPTS, MAX_QUEUE_DEPTH, MAX_SCHEDULES_PER_TENANT,
                          RATE_LIMIT_PER_MINUTE, WEBHOOK_PAYLOAD_RETENTION_DAYS)


class QueueFull(Exception):
    pass


class RateLimited(Exception):
    pass


class IdempotencyConflict(Exception):
    pass


class WorkflowDisabled(Exception):
    pass


class WorkflowScheduleCancelled(Exception):
    pass


class ScheduleLimitReached(Exception):
    pass


class LastWorkspaceOwner(Exception):
    pass


class WorkspaceOwnerActionForbidden(Exception):
    pass


class WebhookSecretRotated(Exception):
    pass


class WebhookEndpointNotFound(Exception):
    pass


class GithubInstallationConflict(Exception):
    pass


class SlackInstallationConflict(Exception):
    pass


def migrate(conn: Connection) -> None:
    from pathlib import Path

    migration_dir = Path(__file__).with_name("migrations")
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('relaycore:migrations', 0))")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version TEXT PRIMARY KEY,
                   applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
               )"""
        )
        for migration in sorted(migration_dir.glob("[0-9]*_*.sql")):
            version = migration.name.split("_", 1)[0]
            if not version.isdigit():
                raise RuntimeError(f"Invalid migration filename: {migration.name}")
            applied = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version=%s", (version,)
            ).fetchone()
            if applied:
                continue
            with conn.transaction():
                conn.execute(migration.read_text(encoding="utf-8"), prepare=False)
                conn.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (version,))


def upsert_oidc_user(
    conn: Connection, issuer: str, subject: str, email: str, display_name: str,
) -> dict[str, Any]:
    return conn.execute(
        """INSERT INTO users(id,issuer,subject,email,display_name)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (issuer,subject) DO UPDATE SET email=EXCLUDED.email,
             display_name=EXCLUDED.display_name,updated_at=clock_timestamp()
           RETURNING id,email,display_name,status""",
        (str(uuid.uuid4()), issuer, subject, email.lower(), display_name[:100]),
    ).fetchone()


def create_auth_session(conn: Connection, user_id: str, request_id: str, ttl_seconds: int) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    conn.execute(
        """INSERT INTO auth_sessions(id,user_id,token_hash,expires_at)
           VALUES (%s,%s,%s,clock_timestamp()+(%s * interval '1 second'))""",
        (str(uuid.uuid4()), user_id, token_hash, ttl_seconds),
    )
    conn.execute(
        "INSERT INTO identity_events(user_id,request_id,kind) VALUES (%s,%s,'auth.login')",
        (user_id, request_id),
    )
    return token


def auth_session(conn: Connection, token: str) -> dict[str, Any] | None:
    if not 32 <= len(token) <= 256:
        return None
    return conn.execute(
        """SELECT u.id AS user_id,u.email,u.display_name,s.token_hash
           FROM auth_sessions s JOIN users u ON u.id=s.user_id
           WHERE s.token_hash=%s AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
             AND u.status='active'""",
        (hashlib.sha256(token.encode()).hexdigest(),),
    ).fetchone()


def revoke_auth_session(conn: Connection, token_hash: str, request_id: str) -> bool:
    session = conn.execute(
        """UPDATE auth_sessions SET revoked_at=clock_timestamp()
           WHERE token_hash=%s AND revoked_at IS NULL RETURNING user_id""",
        (token_hash,),
    ).fetchone()
    if session:
        conn.execute(
            "INSERT INTO identity_events(user_id,request_id,kind) VALUES (%s,%s,'auth.logout')",
            (session["user_id"], request_id),
        )
        return True
    return False


def create_workspace_metrics_token(
    conn: Connection, workspace_id: str, actor_id: str, name: str, token_hash: str, request_id: str,
) -> dict[str, Any]:
    token = conn.execute(
        """INSERT INTO workspace_metrics_tokens(id,workspace_id,name,token_hash,created_by)
           VALUES (%s,%s,%s,%s,%s) RETURNING id,name,created_at""",
        (str(uuid.uuid4()), workspace_id, name, token_hash, actor_id),
    ).fetchone()
    token["id"] = str(token["id"])
    emit_event(conn, workspace_id, "workspace.metrics_token_created", request_id=request_id,
               data={"token_id": token["id"], "name": name, "actor_user_id": actor_id})
    return token


def workspace_metrics_token(conn: Connection, token_hash: str) -> dict[str, Any] | None:
    return conn.execute(
        """SELECT id,workspace_id FROM workspace_metrics_tokens
           WHERE token_hash=%s AND revoked_at IS NULL""", (token_hash,),
    ).fetchone()


def list_workspace_metrics_tokens(conn: Connection, workspace_id: str) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT id,name,created_at,revoked_at FROM workspace_metrics_tokens
           WHERE workspace_id=%s ORDER BY created_at DESC""", (workspace_id,),
    ).fetchall()


def revoke_workspace_metrics_token(
    conn: Connection, workspace_id: str, token_id: str, actor_id: str, request_id: str,
) -> bool:
    changed = conn.execute(
        """UPDATE workspace_metrics_tokens SET revoked_at=clock_timestamp()
           WHERE id=%s AND workspace_id=%s AND revoked_at IS NULL RETURNING id,name""",
        (token_id, workspace_id),
    ).fetchone()
    if not changed:
        return False
    emit_event(conn, workspace_id, "workspace.metrics_token_revoked", request_id=request_id,
               data={"token_id": str(changed["id"]), "name": changed["name"], "actor_user_id": actor_id})
    return True


def list_user_workspaces(conn: Connection, user_id: str) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT w.id,w.name,m.role FROM workspace_members m
           JOIN workspaces w ON w.id=m.workspace_id
           WHERE m.user_id=%s ORDER BY w.created_at,w.id""",
        (user_id,),
    ).fetchall()


def workspace_membership(conn: Connection, workspace_id: str, user_id: str) -> dict[str, Any] | None:
    return conn.execute(
        """SELECT w.id AS workspace_id,m.role FROM workspace_members m
           JOIN workspaces w ON w.id=m.workspace_id
           WHERE m.workspace_id=%s AND m.user_id=%s""",
        (workspace_id, user_id),
    ).fetchone()


def create_github_oauth_state(
    conn: Connection, workspace_id: str, user_id: str, state_hash: str, verifier_encrypted: str,
) -> None:
    conn.execute("DELETE FROM github_oauth_states WHERE expires_at<=clock_timestamp()")
    conn.execute(
        """INSERT INTO github_oauth_states(state_hash,workspace_id,user_id,verifier_encrypted)
           VALUES (%s,%s,%s,%s)""", (state_hash, workspace_id, user_id, verifier_encrypted),
    )


def github_oauth_state(conn: Connection, state_hash: str) -> dict[str, Any] | None:
    return conn.execute(
        """SELECT state_hash,workspace_id,user_id,installation_id,verifier_encrypted
           FROM github_oauth_states WHERE state_hash=%s AND expires_at>clock_timestamp()""",
        (state_hash,),
    ).fetchone()


def discard_github_oauth_state(conn: Connection, state_hash: str) -> None:
    conn.execute("DELETE FROM github_oauth_states WHERE state_hash=%s", (state_hash,))


def bind_github_oauth_installation(conn: Connection, state_hash: str, installation_id: int) -> bool:
    return bool(conn.execute(
        """UPDATE github_oauth_states SET installation_id=%s
           WHERE state_hash=%s AND expires_at>clock_timestamp()
             AND (installation_id IS NULL OR installation_id=%s) RETURNING state_hash""",
        (installation_id, state_hash, installation_id),
    ).fetchone())


def finish_github_installation(
    conn: Connection, state_hash: str, user_id: str, installation_id: int,
    account_id: int, account_login: str, account_type: str, request_id: str, encryption_key: bytes,
) -> dict[str, Any] | None:
    state = conn.execute(
        """SELECT workspace_id,user_id,installation_id FROM github_oauth_states
           WHERE state_hash=%s AND expires_at>clock_timestamp() FOR UPDATE""", (state_hash,),
    ).fetchone()
    if not state or state["user_id"] != user_id or state["installation_id"] != installation_id:
        return None
    member = workspace_membership(conn, state["workspace_id"], user_id)
    if not member or member["role"] not in {"OWNER", "ADMIN"}:
        return None
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                 (f"relaycore:github-workspace:{state['workspace_id']}",))
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                 (f"relaycore:github-installation:{installation_id}",))
    existing = conn.execute(
        "SELECT installation_id,workspace_id,endpoint_id,status FROM github_installations WHERE installation_id=%s FOR UPDATE",
        (installation_id,),
    ).fetchone()
    if existing and existing["workspace_id"] != state["workspace_id"]:
        raise GithubInstallationConflict
    other = conn.execute(
        "SELECT installation_id,endpoint_id,status FROM github_installations WHERE workspace_id=%s FOR UPDATE",
        (state["workspace_id"],),
    ).fetchone()
    if other and other["installation_id"] != installation_id and other["status"] != "revoked":
        raise GithubInstallationConflict
    current = existing or other
    if current and current["status"] != "revoked":
        conn.execute("DELETE FROM github_oauth_states WHERE state_hash=%s", (state_hash,))
        return {"installation_id": installation_id, "endpoint_id": current["endpoint_id"],
                "status": current["status"], "account_login": account_login, "created": False}

    endpoint_id, secret = str(uuid.uuid4()), secrets.token_urlsafe(32)
    endpoint_name = ("GitHub: " + account_login)[:100]
    conn.execute(
        """INSERT INTO webhook_endpoints
           (id,workspace_id,name,created_by,creation_key,creation_fingerprint,source)
           VALUES (%s,%s,%s,%s,%s,%s,'github')""",
        (endpoint_id, state["workspace_id"], endpoint_name, user_id, str(uuid.uuid4()),
         hashlib.sha256(endpoint_name.encode()).hexdigest()),
    )
    conn.execute(
        """INSERT INTO webhook_secrets(endpoint_id,version,encrypted_secret,idempotency_key)
           VALUES (%s,1,%s,'github-internal')""", (endpoint_id, encrypt_secret(secret, encryption_key)),
    )
    if current:
        conn.execute(
            """UPDATE github_installations SET installation_id=%s,endpoint_id=%s,account_id=%s,
                      account_login=%s,account_type=%s,status='active',linked_by=%s,
                      updated_at=clock_timestamp() WHERE installation_id=%s""",
            (installation_id, endpoint_id, account_id, account_login, account_type, user_id,
             current["installation_id"]),
        )
    else:
        conn.execute(
            """INSERT INTO github_installations
               (installation_id,workspace_id,endpoint_id,account_id,account_login,account_type,linked_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (installation_id, state["workspace_id"], endpoint_id, account_id, account_login,
             account_type, user_id),
        )
    conn.execute("DELETE FROM github_oauth_states WHERE state_hash=%s", (state_hash,))
    emit_event(conn, state["workspace_id"], "github.installation_linked", request_id=request_id,
               data={"installation_id": installation_id, "account_login": account_login,
                     "actor_user_id": user_id})
    return {"installation_id": installation_id, "endpoint_id": endpoint_id,
            "status": "active", "account_login": account_login, "created": True}


def github_installation_for_workspace(conn: Connection, workspace_id: str) -> dict[str, Any] | None:
    return conn.execute(
        """SELECT installation_id,endpoint_id,account_id,account_login,account_type,status,created_at
           FROM github_installations WHERE workspace_id=%s ORDER BY created_at DESC LIMIT 1""",
        (workspace_id,),
    ).fetchone()


def github_installation_for_delivery(conn: Connection, installation_id: int) -> dict[str, Any] | None:
    return conn.execute(
        """SELECT g.workspace_id,g.endpoint_id,g.status,s.version
           FROM github_installations g JOIN webhook_secrets s ON s.endpoint_id=g.endpoint_id
           JOIN webhook_endpoints e ON e.id=g.endpoint_id
           WHERE g.installation_id=%s AND s.revoked_at IS NULL AND e.revoked_at IS NULL
           FOR UPDATE OF g""",
        (installation_id,),
    ).fetchone()


def update_github_installation_status(conn: Connection, installation_id: int, action: str,
                                     request_id: str) -> bool:
    statuses = {"suspend": "suspended", "unsuspend": "active", "deleted": "revoked"}
    status = statuses.get(action)
    if not status:
        return False
    row = conn.execute(
        """UPDATE github_installations SET status=%s,updated_at=clock_timestamp()
           WHERE installation_id=%s AND status<>'revoked' AND status IS DISTINCT FROM %s
           RETURNING workspace_id,endpoint_id,status""",
        (status, installation_id, status),
    ).fetchone()
    if not row:
        return False
    if status == "revoked":
        conn.execute("UPDATE webhook_endpoints SET revoked_at=clock_timestamp() WHERE id=%s AND revoked_at IS NULL",
                     (row["endpoint_id"],))
        conn.execute("UPDATE webhook_secrets SET revoked_at=clock_timestamp() WHERE endpoint_id=%s AND revoked_at IS NULL",
                     (row["endpoint_id"],))
    emit_event(conn, row["workspace_id"], "github.installation_status_changed",
               request_id=request_id, data={"installation_id": installation_id, "status": status})
    return True


def revoke_github_installation(conn: Connection, workspace_id: str, request_id: str) -> bool:
    row = conn.execute(
        """UPDATE github_installations SET status='revoked',updated_at=clock_timestamp()
           WHERE workspace_id=%s AND status<>'revoked' RETURNING installation_id,endpoint_id""",
        (workspace_id,),
    ).fetchone()
    if not row:
        return False
    conn.execute("UPDATE webhook_endpoints SET revoked_at=clock_timestamp() WHERE id=%s AND revoked_at IS NULL",
                 (row["endpoint_id"],))
    conn.execute("UPDATE webhook_secrets SET revoked_at=clock_timestamp() WHERE endpoint_id=%s AND revoked_at IS NULL",
                 (row["endpoint_id"],))
    conn.execute("DELETE FROM github_oauth_states WHERE workspace_id=%s", (workspace_id,))
    emit_event(conn, workspace_id, "github.installation_disconnected", request_id=request_id,
               data={"installation_id": row["installation_id"]})
    return True


def create_slack_oauth_state(conn: Connection, workspace_id: str, user_id: str, state_hash: str) -> None:
    conn.execute("DELETE FROM slack_oauth_states WHERE expires_at<=clock_timestamp()")
    conn.execute(
        "INSERT INTO slack_oauth_states(state_hash,workspace_id,user_id) VALUES (%s,%s,%s)",
        (state_hash, workspace_id, user_id),
    )


def slack_oauth_state(conn: Connection, state_hash: str) -> dict[str, Any] | None:
    return conn.execute(
        """SELECT workspace_id,user_id FROM slack_oauth_states
           WHERE state_hash=%s AND expires_at>clock_timestamp()""", (state_hash,),
    ).fetchone()


def discard_slack_oauth_state(conn: Connection, state_hash: str) -> None:
    conn.execute("DELETE FROM slack_oauth_states WHERE state_hash=%s", (state_hash,))


def finish_slack_installation(
    conn: Connection, state_hash: str, user_id: str, team_id: str, team_name: str, bot_user_id: str,
    bot_token: str, request_id: str, encryption_key: bytes,
) -> dict[str, Any] | None:
    state = conn.execute(
        """SELECT workspace_id,user_id FROM slack_oauth_states
           WHERE state_hash=%s AND expires_at>clock_timestamp() FOR UPDATE""", (state_hash,),
    ).fetchone()
    if not state or state["user_id"] != user_id:
        return None
    member = workspace_membership(conn, state["workspace_id"], user_id)
    if not member or member["role"] not in {"OWNER", "ADMIN"}:
        return None
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                 (f"relaycore:slack-workspace:{state['workspace_id']}",))
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                 (f"relaycore:slack-team:{team_id}",))
    active = conn.execute(
        """SELECT id,workspace_id,team_id,endpoint_id,credential_id
           FROM slack_installations WHERE status='active'
             AND (workspace_id=%s OR team_id=%s) FOR UPDATE""",
        (state["workspace_id"], team_id),
    ).fetchall()
    current = next((row for row in active if row["workspace_id"] == state["workspace_id"]
                    and row["team_id"] == team_id), None)
    if active and (len(active) != 1 or current is None):
        raise SlackInstallationConflict
    if current:
        rotated = rotate_workspace_credential(
            conn, state["workspace_id"], current["credential_id"], user_id, bot_token,
            f"slack-oauth:{state_hash}", request_id, encryption_key,
        )
        if not rotated:
            raise SlackInstallationConflict
        conn.execute("""UPDATE slack_installations SET team_name=%s,bot_user_id=%s,linked_by=%s,
                          updated_at=clock_timestamp() WHERE id=%s""",
                     (team_name, bot_user_id, user_id, current["id"]))
        endpoint_id = current["endpoint_id"]
        emit_event(conn, state["workspace_id"], "slack.installation_reconnected", request_id=request_id,
                   data={"team_id": team_id, "actor_user_id": user_id})
    else:
        credential = create_workspace_credential(
            conn, state["workspace_id"], user_id, "slack", ("Slack · " + team_name)[:100],
            bot_token, None, f"slack-install:{state_hash}", request_id, encryption_key,
        )
        endpoint_id = str(uuid.uuid4())
        endpoint_name = ("Slack: " + team_name)[:100]
        conn.execute(
            """INSERT INTO webhook_endpoints
               (id,workspace_id,name,created_by,creation_key,creation_fingerprint,source)
               VALUES (%s,%s,%s,%s,%s,%s,'slack')""",
            (endpoint_id, state["workspace_id"], endpoint_name, user_id, str(uuid.uuid4()),
             hashlib.sha256(endpoint_name.encode()).hexdigest()),
        )
        conn.execute(
            """INSERT INTO webhook_secrets(endpoint_id,version,encrypted_secret,idempotency_key)
               VALUES (%s,1,%s,'slack-internal')""",
            (endpoint_id, encrypt_secret(secrets.token_urlsafe(32), encryption_key)),
        )
        conn.execute(
            """INSERT INTO slack_installations
               (id,team_id,workspace_id,endpoint_id,credential_id,team_name,bot_user_id,linked_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (str(uuid.uuid4()), team_id, state["workspace_id"], endpoint_id, credential["id"],
             team_name, bot_user_id, user_id),
        )
        emit_event(conn, state["workspace_id"], "slack.installation_linked", request_id=request_id,
                   data={"team_id": team_id, "team_name": team_name, "actor_user_id": user_id})
    conn.execute("DELETE FROM slack_oauth_states WHERE state_hash=%s", (state_hash,))
    return {"team_id": team_id, "team_name": team_name, "endpoint_id": endpoint_id,
            "status": "active", "created": current is None}


def slack_installation_for_workspace(conn: Connection, workspace_id: str) -> dict[str, Any] | None:
    return conn.execute(
        """SELECT team_id,team_name,endpoint_id,status,created_at FROM slack_installations
           WHERE workspace_id=%s ORDER BY created_at DESC LIMIT 1""", (workspace_id,),
    ).fetchone()


def slack_action_credential_id(conn: Connection, workspace_id: str) -> str | None:
    row = conn.execute(
        """SELECT s.credential_id FROM slack_installations s
           JOIN integration_credentials c ON c.id=s.credential_id
           JOIN integration_credential_secrets v ON v.credential_id=c.id
           WHERE s.workspace_id=%s AND s.status='active' AND c.provider='slack'
             AND c.revoked_at IS NULL AND v.revoked_at IS NULL""",
        (workspace_id,),
    ).fetchone()
    return row["credential_id"] if row else None


def slack_installation_for_delivery(conn: Connection, team_id: str) -> dict[str, Any] | None:
    return conn.execute(
        """SELECT s.workspace_id,s.endpoint_id,s.credential_id,w.version
           FROM slack_installations s JOIN webhook_secrets w ON w.endpoint_id=s.endpoint_id
           JOIN webhook_endpoints e ON e.id=s.endpoint_id
           WHERE s.team_id=%s AND s.status='active' AND w.revoked_at IS NULL AND e.revoked_at IS NULL
           FOR UPDATE OF s""", (team_id,),
    ).fetchone()


def _revoke_slack_installation(conn: Connection, row: dict[str, Any], request_id: str) -> None:
    conn.execute("UPDATE webhook_endpoints SET revoked_at=clock_timestamp() WHERE id=%s AND revoked_at IS NULL",
                 (row["endpoint_id"],))
    conn.execute("""UPDATE webhook_secrets SET revoked_at=clock_timestamp()
                   WHERE endpoint_id=%s AND revoked_at IS NULL""", (row["endpoint_id"],))
    conn.execute("""UPDATE integration_credentials SET revoked_at=clock_timestamp()
                   WHERE id=%s AND revoked_at IS NULL""", (row["credential_id"],))
    conn.execute("""UPDATE integration_credential_secrets SET revoked_at=clock_timestamp()
                   WHERE credential_id=%s AND revoked_at IS NULL""", (row["credential_id"],))
    emit_event(conn, row["workspace_id"], "slack.installation_disconnected", request_id=request_id,
               data={"team_id": row["team_id"]})


def update_slack_installation_status(conn: Connection, team_id: str, request_id: str) -> bool:
    row = conn.execute(
        """UPDATE slack_installations SET status='revoked',updated_at=clock_timestamp()
           WHERE team_id=%s AND status='active'
           RETURNING workspace_id,team_id,endpoint_id,credential_id""", (team_id,),
    ).fetchone()
    if not row:
        return False
    _revoke_slack_installation(conn, row, request_id)
    return True


def revoke_slack_installation(conn: Connection, workspace_id: str, request_id: str) -> bool:
    row = conn.execute(
        """UPDATE slack_installations SET status='revoked',updated_at=clock_timestamp()
           WHERE workspace_id=%s AND status='active'
           RETURNING workspace_id,team_id,endpoint_id,credential_id""", (workspace_id,),
    ).fetchone()
    if not row:
        return False
    _revoke_slack_installation(conn, row, request_id)
    conn.execute("DELETE FROM slack_oauth_states WHERE workspace_id=%s", (workspace_id,))
    return True


def create_workspace(conn: Connection, user_id: str, name: str, request_id: str) -> dict[str, Any]:
    workspace_id = str(uuid.uuid4())
    conn.execute("INSERT INTO workspaces(id,name,created_by) VALUES (%s,%s,%s)",
                 (workspace_id, name.strip(), user_id))
    conn.execute(
        "INSERT INTO workspace_members(workspace_id,user_id,role,created_by) VALUES (%s,%s,'OWNER',%s)",
        (workspace_id, user_id, user_id),
    )
    emit_event(conn, workspace_id, "workspace.created", request_id=request_id,
               data={"workspace_id": workspace_id, "actor_user_id": user_id})
    return {"id": workspace_id, "name": name.strip(), "role": "OWNER"}


def workspace_members(conn: Connection, workspace_id: str) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT u.id AS user_id,u.email,u.display_name,m.role,m.created_at,m.updated_at
           FROM workspace_members m JOIN users u ON u.id=m.user_id
           WHERE m.workspace_id=%s ORDER BY m.created_at,u.id""",
        (workspace_id,),
    ).fetchall()


def set_workspace_member(
    conn: Connection, workspace_id: str, actor_id: str, target_id: str, role: str, request_id: str,
) -> bool:
    conn.execute("SELECT id FROM workspaces WHERE id=%s FOR UPDATE", (workspace_id,))
    target = conn.execute("SELECT id FROM users WHERE id=%s", (target_id,)).fetchone()
    if not target:
        return False
    existing = conn.execute(
        "SELECT role FROM workspace_members WHERE workspace_id=%s AND user_id=%s FOR UPDATE",
        (workspace_id, target_id),
    ).fetchone()
    if existing and existing["role"] == "OWNER" and actor_id != target_id:
        actor = conn.execute(
            "SELECT role FROM workspace_members WHERE workspace_id=%s AND user_id=%s",
            (workspace_id, actor_id),
        ).fetchone()
        if not actor or actor["role"] != "OWNER":
            raise WorkspaceOwnerActionForbidden
    if existing and existing["role"] == "OWNER" and role != "OWNER":
        owners = conn.execute(
            "SELECT count(*) AS n FROM workspace_members WHERE workspace_id=%s AND role='OWNER'",
            (workspace_id,),
        ).fetchone()["n"]
        if owners < 2:
            raise LastWorkspaceOwner
    conn.execute(
        """INSERT INTO workspace_members(workspace_id,user_id,role,created_by)
           VALUES (%s,%s,%s,%s) ON CONFLICT (workspace_id,user_id)
           DO UPDATE SET role=EXCLUDED.role,updated_at=clock_timestamp()""",
        (workspace_id, target_id, role, actor_id),
    )
    emit_event(conn, workspace_id, "workspace.member_role_changed" if existing else "workspace.member_added",
               request_id=request_id,
               data={"actor_user_id": actor_id, "target_user_id": target_id, "role": role})
    return True


def remove_workspace_member(
    conn: Connection, workspace_id: str, actor_id: str, target_id: str, request_id: str,
) -> bool:
    conn.execute("SELECT id FROM workspaces WHERE id=%s FOR UPDATE", (workspace_id,))
    member = conn.execute(
        "SELECT role FROM workspace_members WHERE workspace_id=%s AND user_id=%s FOR UPDATE",
        (workspace_id, target_id),
    ).fetchone()
    if not member:
        return False
    if member["role"] == "OWNER":
        actor = conn.execute(
            "SELECT role FROM workspace_members WHERE workspace_id=%s AND user_id=%s",
            (workspace_id, actor_id),
        ).fetchone()
        if not actor or actor["role"] != "OWNER":
            raise WorkspaceOwnerActionForbidden
        owners = conn.execute(
            "SELECT count(*) AS n FROM workspace_members WHERE workspace_id=%s AND role='OWNER'",
            (workspace_id,),
        ).fetchone()["n"]
        if owners < 2:
            raise LastWorkspaceOwner
    conn.execute("DELETE FROM workspace_members WHERE workspace_id=%s AND user_id=%s",
                 (workspace_id, target_id))
    emit_event(conn, workspace_id, "workspace.member_removed", request_id=request_id,
               data={"actor_user_id": actor_id, "target_user_id": target_id})
    return True


def emit_event(
    conn: Connection,
    tenant_id: str,
    kind: str,
    *,
    run_id: str | None = None,
    task_id: str | None = None,
    worker_id: str | None = None,
    request_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    # ponytail: serialize events per tenant for commit-ordered SSE cursors; replace with an ordered outbox if this becomes a write bottleneck.
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"relaycore:event-order:{tenant_id}",),
    )
    conn.execute(
        """INSERT INTO events(tenant_id,run_id,task_id,worker_id,request_id,kind,data)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (tenant_id, run_id, task_id, worker_id, request_id, kind, Jsonb(data or {})),
    )


def create_webhook_endpoint(
    conn: Connection, workspace_id: str, actor_id: str, name: str, idempotency_key: str,
    request_id: str, encryption_key: bytes,
) -> dict[str, Any]:
    fingerprint = hashlib.sha256(name.encode()).hexdigest()
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                 (f"relaycore:webhook-create:{workspace_id}",))
    existing = conn.execute(
        """SELECT id,name,creation_fingerprint,created_at,revoked_at FROM webhook_endpoints
           WHERE workspace_id=%s AND creation_key=%s FOR UPDATE""",
        (workspace_id, idempotency_key),
    ).fetchone()
    if existing:
        if existing["creation_fingerprint"] != fingerprint or existing["revoked_at"]:
            raise IdempotencyConflict
        active = conn.execute(
            """SELECT encrypted_secret FROM webhook_secrets
               WHERE endpoint_id=%s AND revoked_at IS NULL""", (existing["id"],)
        ).fetchone()
        if not active:
            raise IdempotencyConflict
        return {"id": existing["id"], "name": existing["name"], "created_at": existing["created_at"],
                "secret": decrypt_secret(active["encrypted_secret"], encryption_key), "created": False}

    endpoint_id, secret = str(uuid.uuid4()), secrets.token_urlsafe(32)
    conn.execute(
        """INSERT INTO webhook_endpoints
           (id,workspace_id,name,created_by,creation_key,creation_fingerprint)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        (endpoint_id, workspace_id, name, actor_id, idempotency_key, fingerprint),
    )
    conn.execute(
        """INSERT INTO webhook_secrets(endpoint_id,version,encrypted_secret,idempotency_key)
           VALUES (%s,1,%s,%s)""",
        (endpoint_id, encrypt_secret(secret, encryption_key), f"create:{idempotency_key}"),
    )
    emit_event(conn, workspace_id, "webhook.endpoint_created", request_id=request_id,
               data={"endpoint_id": endpoint_id, "actor_user_id": actor_id})
    return {"id": endpoint_id, "name": name, "secret": secret, "created": True}


def list_webhook_endpoints(conn: Connection, workspace_id: str) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT id,name,created_at,revoked_at,last_received_at
           FROM webhook_endpoints WHERE workspace_id=%s AND source='custom' ORDER BY created_at DESC""",
        (workspace_id,),
    ).fetchall()


def create_workspace_credential(
    conn: Connection, workspace_id: str, actor_id: str, provider: str, name: str, secret: str,
    allowed_host: str | None,
    idempotency_key: str, request_id: str, encryption_key: bytes,
) -> dict[str, Any]:
    fingerprint = credential_fingerprint(encryption_key, provider, name, allowed_host, secret)
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                 (f"relaycore:credential-create:{workspace_id}:{idempotency_key}",))
    existing = conn.execute(
        """SELECT id,provider,name,allowed_host,creation_fingerprint,created_at,revoked_at
           FROM integration_credentials WHERE workspace_id=%s AND creation_key=%s FOR UPDATE""",
        (workspace_id, idempotency_key),
    ).fetchone()
    if existing:
        if existing["creation_fingerprint"] != fingerprint or existing["revoked_at"]:
            raise IdempotencyConflict
        return {key: existing[key] for key in ("id", "provider", "name", "allowed_host", "created_at")} | {
            "created": False}

    credential_id = str(uuid.uuid4())
    created_at = conn.execute(
        """INSERT INTO integration_credentials
           (id,workspace_id,provider,name,allowed_host,created_by,creation_key,creation_fingerprint)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING created_at""",
        (credential_id, workspace_id, provider, name, allowed_host, actor_id, idempotency_key, fingerprint),
    ).fetchone()
    conn.execute(
        """INSERT INTO integration_credential_secrets
           (credential_id,version,encrypted_secret,idempotency_key,secret_fingerprint)
           VALUES (%s,1,%s,%s,%s)""",
        (credential_id, encrypt_secret(secret, encryption_key), f"create:{idempotency_key}", fingerprint),
    )
    emit_event(conn, workspace_id, "credential.created", request_id=request_id,
               data={"credential_id": credential_id, "provider": provider, "actor_user_id": actor_id})
    return {"id": credential_id, "provider": provider, "name": name, "allowed_host": allowed_host,
            "created_at": created_at["created_at"], "created": True}


def list_workspace_credentials(conn: Connection, workspace_id: str) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT id,provider,name,allowed_host,created_at,revoked_at,
                  (SELECT max(version) FROM integration_credential_secrets s WHERE s.credential_id=c.id) AS version
           FROM integration_credentials c WHERE workspace_id=%s ORDER BY created_at DESC""",
        (workspace_id,),
    ).fetchall()


def rotate_workspace_credential(
    conn: Connection, workspace_id: str, credential_id: str, actor_id: str, secret: str,
    idempotency_key: str, request_id: str, encryption_key: bytes,
) -> dict[str, Any] | None:
    credential = conn.execute(
        """SELECT id,provider,name,allowed_host,revoked_at FROM integration_credentials
           WHERE id=%s AND workspace_id=%s FOR UPDATE""", (credential_id, workspace_id)
    ).fetchone()
    if not credential or credential["revoked_at"]:
        return None
    fingerprint = credential_fingerprint(
        encryption_key, credential["provider"], credential["name"], credential["allowed_host"], secret,
    )
    previous = conn.execute(
        """SELECT version,secret_fingerprint,revoked_at FROM integration_credential_secrets
           WHERE credential_id=%s AND idempotency_key=%s FOR UPDATE""", (credential_id, idempotency_key)
    ).fetchone()
    if previous:
        if previous["secret_fingerprint"] != fingerprint or previous["revoked_at"]:
            raise IdempotencyConflict
        return {"id": credential_id, "provider": credential["provider"], "name": credential["name"],
                "version": previous["version"], "created": False}
    current = conn.execute(
        """SELECT version FROM integration_credential_secrets
           WHERE credential_id=%s AND revoked_at IS NULL FOR UPDATE""", (credential_id,)
    ).fetchone()
    if not current:
        raise IdempotencyConflict
    version = current["version"] + 1
    conn.execute("UPDATE integration_credential_secrets SET revoked_at=clock_timestamp() "
                 "WHERE credential_id=%s AND revoked_at IS NULL", (credential_id,))
    conn.execute(
        """INSERT INTO integration_credential_secrets
           (credential_id,version,encrypted_secret,idempotency_key,secret_fingerprint)
           VALUES (%s,%s,%s,%s,%s)""",
        (credential_id, version, encrypt_secret(secret, encryption_key), idempotency_key, fingerprint),
    )
    emit_event(conn, workspace_id, "credential.rotated", request_id=request_id,
               data={"credential_id": credential_id, "provider": credential["provider"],
                     "version": version, "actor_user_id": actor_id})
    return {"id": credential_id, "provider": credential["provider"], "name": credential["name"],
            "version": version, "created": True}


def revoke_workspace_credential(
    conn: Connection, workspace_id: str, credential_id: str, actor_id: str, request_id: str,
) -> bool:
    changed = conn.execute(
        """UPDATE integration_credentials SET revoked_at=clock_timestamp()
           WHERE id=%s AND workspace_id=%s AND revoked_at IS NULL RETURNING id,provider""",
        (credential_id, workspace_id),
    ).fetchone()
    if not changed:
        return False
    conn.execute("UPDATE integration_credential_secrets SET revoked_at=clock_timestamp() "
                 "WHERE credential_id=%s AND revoked_at IS NULL", (credential_id,))
    emit_event(conn, workspace_id, "credential.revoked", request_id=request_id,
               data={"credential_id": credential_id, "provider": changed["provider"],
                     "actor_user_id": actor_id})
    return True


def workspace_credential_secret(
    conn: Connection, workspace_id: str, credential_id: str, encryption_key: bytes,
) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT c.provider,c.allowed_host,s.encrypted_secret FROM integration_credentials c
           JOIN integration_credential_secrets s ON s.credential_id=c.id
           WHERE c.id=%s AND c.workspace_id=%s AND c.revoked_at IS NULL AND s.revoked_at IS NULL""",
        (credential_id, workspace_id),
    ).fetchone()
    if not row:
        return None
    return {"provider": row["provider"], "allowed_host": row["allowed_host"],
            "secret": decrypt_secret(row["encrypted_secret"], encryption_key)}


def active_workspace_credential(conn: Connection, workspace_id: str, credential_id: str,
                                provider: str, allowed_host: str | None = None) -> bool:
    return bool(conn.execute(
        """SELECT 1 FROM integration_credentials c
           JOIN integration_credential_secrets s ON s.credential_id=c.id
           WHERE c.id=%s AND c.workspace_id=%s AND c.provider=%s
             AND c.allowed_host IS NOT DISTINCT FROM %s
             AND c.revoked_at IS NULL AND s.revoked_at IS NULL""",
        (credential_id, workspace_id, provider, allowed_host),
    ).fetchone())


def rotate_webhook_secret(
    conn: Connection, workspace_id: str, endpoint_id: str, actor_id: str, idempotency_key: str,
    request_id: str, encryption_key: bytes,
) -> dict[str, Any] | None:
    endpoint = conn.execute(
        """SELECT id,revoked_at,source FROM webhook_endpoints
           WHERE id=%s AND workspace_id=%s FOR UPDATE""", (endpoint_id, workspace_id)
    ).fetchone()
    if not endpoint or endpoint["revoked_at"] or endpoint["source"] != "custom":
        return None
    previous = conn.execute(
        """SELECT version,encrypted_secret,revoked_at FROM webhook_secrets
           WHERE endpoint_id=%s AND idempotency_key=%s FOR UPDATE""", (endpoint_id, idempotency_key)
    ).fetchone()
    if previous:
        if previous["revoked_at"]:
            raise IdempotencyConflict
        return {"endpoint_id": endpoint_id, "version": previous["version"],
                "secret": decrypt_secret(previous["encrypted_secret"], encryption_key), "created": False}
    current = conn.execute(
        """SELECT version FROM webhook_secrets
           WHERE endpoint_id=%s AND revoked_at IS NULL FOR UPDATE""", (endpoint_id,)
    ).fetchone()
    if not current:
        raise WebhookSecretRotated
    current_version = current["version"]
    conn.execute(
        """UPDATE webhook_secrets SET revoked_at=clock_timestamp()
           WHERE endpoint_id=%s AND revoked_at IS NULL""", (endpoint_id,)
    )
    secret, version = secrets.token_urlsafe(32), current_version + 1
    conn.execute(
        """INSERT INTO webhook_secrets(endpoint_id,version,encrypted_secret,idempotency_key)
           VALUES (%s,%s,%s,%s)""",
        (endpoint_id, version, encrypt_secret(secret, encryption_key), idempotency_key),
    )
    emit_event(conn, workspace_id, "webhook.secret_rotated", request_id=request_id,
               data={"endpoint_id": endpoint_id, "version": version, "actor_user_id": actor_id})
    return {"endpoint_id": endpoint_id, "version": version, "secret": secret, "created": True}


def revoke_webhook_endpoint(
    conn: Connection, workspace_id: str, endpoint_id: str, actor_id: str, request_id: str,
) -> bool:
    endpoint = conn.execute(
        """UPDATE webhook_endpoints SET revoked_at=clock_timestamp()
           WHERE id=%s AND workspace_id=%s AND source='custom' AND revoked_at IS NULL RETURNING id""",
        (endpoint_id, workspace_id),
    ).fetchone()
    if not endpoint:
        return False
    conn.execute(
        """UPDATE webhook_secrets SET revoked_at=clock_timestamp()
           WHERE endpoint_id=%s AND revoked_at IS NULL""", (endpoint_id,)
    )
    emit_event(conn, workspace_id, "webhook.endpoint_revoked", request_id=request_id,
               data={"endpoint_id": endpoint_id, "actor_user_id": actor_id})
    return True


def webhook_signing_info(conn: Connection, endpoint_id: str) -> dict[str, Any] | None:
    return conn.execute(
        """SELECT e.workspace_id,e.source,s.version,s.encrypted_secret
           FROM webhook_endpoints e JOIN webhook_secrets s ON s.endpoint_id=e.id
           WHERE e.id=%s AND e.revoked_at IS NULL AND s.revoked_at IS NULL""", (endpoint_id,)
    ).fetchone()


def persist_webhook_event(
    conn: Connection, workspace_id: str, endpoint_id: str, secret_version: int, event_key: str,
    request_id: str, body: bytes, payload: dict[str, Any], *, allow_workflow_triggers: bool = False,
    allow_http_workflow_triggers: bool = False,
) -> dict[str, Any]:
    endpoint = conn.execute(
        """SELECT id FROM webhook_endpoints WHERE id=%s AND workspace_id=%s
           AND revoked_at IS NULL FOR UPDATE""", (endpoint_id, workspace_id)
    ).fetchone()
    active = conn.execute(
        """SELECT version FROM webhook_secrets WHERE endpoint_id=%s AND revoked_at IS NULL FOR UPDATE""",
        (endpoint_id,),
    ).fetchone()
    if not endpoint or not active:
        raise WebhookSecretRotated
    if active["version"] != secret_version:
        raise WebhookSecretRotated
    payload_hash = hashlib.sha256(body).hexdigest()
    event_type = payload.get("type")
    stored_event_type = event_type if isinstance(event_type, str) and len(event_type) <= 120 else None
    event_id = str(uuid.uuid4())
    inserted = conn.execute(
        """INSERT INTO incoming_events
           (id,workspace_id,endpoint_id,event_key,request_id,payload_sha256,raw_body,payload,event_type)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (endpoint_id,event_key) DO NOTHING
           RETURNING id""",
        (event_id, workspace_id, endpoint_id, event_key, request_id, payload_hash, body,
         Jsonb(payload), stored_event_type),
    ).fetchone()
    if not inserted:
        previous = conn.execute(
            """SELECT id,payload_sha256 FROM incoming_events
               WHERE endpoint_id=%s AND event_key=%s""", (endpoint_id, event_key)
        ).fetchone()
        if previous["payload_sha256"] != payload_hash:
            raise IdempotencyConflict
        return {"id": previous["id"], "duplicate": True}
    conn.execute("UPDATE webhook_endpoints SET last_received_at=clock_timestamp() WHERE id=%s", (endpoint_id,))
    emit_event(conn, workspace_id, "webhook.received", request_id=request_id,
               data={"event_id": event_id, "endpoint_id": endpoint_id,
                     "event_key": event_key, "payload_sha256": payload_hash})
    triggered_runs = []
    event_type = payload.get("type")
    if (allow_workflow_triggers or allow_http_workflow_triggers) and isinstance(event_type, str):
        matches = conn.execute(
            """SELECT d.id AS workflow_id,v.id AS version_id,v.version_number,
                      v.definition,v.definition_hash
               FROM workflow_definitions d JOIN workflow_versions v
                 ON v.tenant_id=d.tenant_id AND v.workflow_id=d.id AND v.version_number=d.current_version
               WHERE d.tenant_id=%s AND d.status='active'
                 AND v.definition->'trigger'->>'type'='webhook'
                 AND v.definition->'trigger'->>'endpoint_id'=%s
                 AND v.definition->'trigger'->>'event_type'=%s
               ORDER BY d.id""",
            (workspace_id, endpoint_id, event_type),
        ).fetchall()
        for match in matches:
            actions = {step["action"] for step in match["definition"]["steps"]}
            if actions == {"http"}:
                if not allow_http_workflow_triggers:
                    continue
            elif not allow_workflow_triggers or "http" in actions:
                continue
            seed = f"{endpoint_id}:{event_key}:{match['version_id']}".encode()
            run_key = "webhook-trigger:" + hashlib.sha256(seed).hexdigest()
            definition = match["definition"]
            run = create_workflow(
                conn, workspace_id, definition["title"], definition["steps"], run_key, request_id,
                workflow_version_id=match["version_id"], definition_hash=match["definition_hash"],
                trigger_event_id=event_id,
            )
            triggered_runs.append({"workflow_id": match["workflow_id"], "run_id": run["id"],
                                   "workflow_version_id": match["version_id"],
                                   "version": match["version_number"], "created": run["created"]})
    return {"id": event_id, "duplicate": False, "triggered_runs": triggered_runs}


def list_incoming_events(
    conn: Connection, workspace_id: str, limit: int,
    before: tuple[Any, str] | None = None,
) -> list[dict[str, Any]]:
    query = """SELECT i.id,i.endpoint_id,e.name AS endpoint_name,i.event_key,
                      i.request_id,i.payload_sha256,i.event_type,i.received_at
               FROM incoming_events i JOIN webhook_endpoints e ON e.id=i.endpoint_id
               WHERE i.workspace_id=%s"""
    params: tuple[Any, ...] = (workspace_id,)
    if before:
        query += " AND (i.received_at,i.id)<(%s,%s)"
        params += before
    return conn.execute(query + " ORDER BY i.received_at DESC,i.id DESC LIMIT %s",
                        (*params, limit + 1)).fetchall()


def expire_webhook_payloads(conn: Connection, batch_size: int = 500) -> int:
    with conn.transaction():
        candidate_ids = [row["id"] for row in conn.execute(
            """SELECT i.id FROM incoming_events i
               WHERE i.payload IS NOT NULL
                 AND i.received_at < clock_timestamp()-(%s * interval '1 day')
                 AND NOT EXISTS (
                     SELECT 1 FROM workflow_runs w
                     WHERE w.tenant_id=i.workspace_id AND w.trigger_event_id=i.id
                       AND w.status NOT IN ('completed','failed','cancelled')
                 )
               ORDER BY i.received_at,i.id LIMIT %s""",
            (WEBHOOK_PAYLOAD_RETENTION_DAYS, batch_size),
        ).fetchall()]
        if not candidate_ids:
            return 0
        # Lock runs before events so replay and cleanup serialize in the same order.
        conn.execute(
            "SELECT id FROM workflow_runs WHERE trigger_event_id=ANY(%s) ORDER BY id FOR UPDATE",
            (candidate_ids,),
        ).fetchall()
        rows = conn.execute(
            """WITH expired AS (
                   SELECT i.id FROM incoming_events i
                   WHERE i.id=ANY(%s) AND i.payload IS NOT NULL
                     AND i.received_at < clock_timestamp()-(%s * interval '1 day')
                     AND NOT EXISTS (
                         SELECT 1 FROM workflow_runs w
                         WHERE w.tenant_id=i.workspace_id AND w.trigger_event_id=i.id
                           AND w.status NOT IN ('completed','failed','cancelled')
                     )
                   ORDER BY i.received_at,i.id FOR UPDATE OF i SKIP LOCKED LIMIT %s
               )
               UPDATE incoming_events i SET raw_body=NULL,payload=NULL
               FROM expired WHERE i.id=expired.id RETURNING i.id""",
            (candidate_ids, WEBHOOK_PAYLOAD_RETENTION_DAYS, batch_size),
        ).fetchall()
    return len(rows)


def _admit(conn: Connection, tenant_id: str) -> None:
    admit_rate_limit(conn, tenant_id)
    pending = conn.execute(
        "SELECT count(*) AS count FROM tasks WHERE tenant_id=%s AND status IN ('queued','retry_wait','running')",
        (tenant_id,),
    ).fetchone()["count"]
    if pending >= MAX_QUEUE_DEPTH:
        raise QueueFull


def admit_rate_limit(conn: Connection, tenant_id: str) -> None:
    conn.execute(
        "DELETE FROM rate_limits WHERE window_start<date_trunc('minute',clock_timestamp())-interval '1 hour'"
    )
    rate = conn.execute(
        """INSERT INTO rate_limits(tenant_id,window_start,request_count)
           VALUES (%s,date_trunc('minute',clock_timestamp()),1)
           ON CONFLICT (tenant_id,window_start) DO UPDATE
             SET request_count=rate_limits.request_count+1
           RETURNING request_count""",
        (tenant_id,),
    ).fetchone()["request_count"]
    if rate > RATE_LIMIT_PER_MINUTE:
        raise RateLimited


def _lock_tenant(conn: Connection, tenant_id: str) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (tenant_id,))


def _workflow_definition(title: str, steps: list[dict[str, Any]],
                         trigger: dict[str, Any] | None = None) -> dict[str, Any]:
    definition = {"title": title, "steps": steps}
    if trigger:
        definition["trigger"] = {"type": "webhook", **trigger}
    return definition


def _definition_fingerprint(title: str, steps: list[dict[str, Any]],
                            trigger: dict[str, Any] | None = None) -> str:
    return hashlib.sha256(
        json.dumps(_workflow_definition(title, steps, trigger), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _validate_workflow_trigger(
    conn: Connection, tenant_id: str, trigger: dict[str, Any] | None,
) -> None:
    if trigger and not conn.execute(
        """SELECT 1 FROM webhook_endpoints WHERE id=%s AND workspace_id=%s AND revoked_at IS NULL""",
        (str(trigger["endpoint_id"]), tenant_id),
    ).fetchone():
        raise WebhookEndpointNotFound


def create_workflow_definition(
    conn: Connection,
    tenant_id: str,
    title: str,
    steps: list[dict[str, Any]],
    idempotency_key: str,
    author_credential_fingerprint: str,
    request_id: str,
    author_user_id: str | None = None,
    trigger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fingerprint = _definition_fingerprint(title, steps, trigger)
    _lock_tenant(conn, tenant_id)
    existing = conn.execute(
        "SELECT id,fingerprint FROM workflow_definitions WHERE tenant_id=%s AND idempotency_key=%s",
        (tenant_id, idempotency_key),
    ).fetchone()
    if existing:
        if existing["fingerprint"] != fingerprint:
            raise IdempotencyConflict
        version = conn.execute(
            "SELECT id FROM workflow_versions WHERE tenant_id=%s AND workflow_id=%s AND version_number=1",
            (tenant_id, existing["id"]),
        ).fetchone()
        return {"id": existing["id"], "version_id": version["id"], "version": 1, "created": False}

    _validate_workflow_trigger(conn, tenant_id, trigger)
    workflow_id, version_id = str(uuid.uuid4()), str(uuid.uuid4())
    definition = _workflow_definition(title, steps, trigger)
    conn.execute(
        """INSERT INTO workflow_definitions
           (id,tenant_id,title,idempotency_key,fingerprint,author_credential_fingerprint,author_user_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (workflow_id, tenant_id, title, idempotency_key, fingerprint, author_credential_fingerprint, author_user_id),
    )
    conn.execute(
        """INSERT INTO workflow_versions
           (id,tenant_id,workflow_id,version_number,definition,definition_hash,idempotency_key,
            author_credential_fingerprint,author_user_id)
           VALUES (%s,%s,%s,1,%s,%s,%s,%s,%s)""",
        (version_id, tenant_id, workflow_id, Jsonb(definition), fingerprint,
         idempotency_key, author_credential_fingerprint, author_user_id),
    )
    emit_event(conn, tenant_id, "workflow.version_published", request_id=request_id,
               data={"workflow_id": workflow_id, "workflow_version_id": version_id, "version": 1})
    return {"id": workflow_id, "version_id": version_id, "version": 1, "created": True}


def add_workflow_version(
    conn: Connection,
    tenant_id: str,
    workflow_id: str,
    title: str,
    steps: list[dict[str, Any]],
    idempotency_key: str,
    author_credential_fingerprint: str,
    request_id: str,
    author_user_id: str | None = None,
    trigger: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    fingerprint = _definition_fingerprint(title, steps, trigger)
    _lock_tenant(conn, tenant_id)
    existing = conn.execute(
        """SELECT id,version_number,definition_hash FROM workflow_versions
           WHERE tenant_id=%s AND workflow_id=%s AND idempotency_key=%s""",
        (tenant_id, workflow_id, idempotency_key),
    ).fetchone()
    if existing:
        if existing["definition_hash"] != fingerprint:
            raise IdempotencyConflict
        return {"id": existing["id"], "version": existing["version_number"], "created": False}

    workflow = conn.execute(
        """SELECT current_version,status FROM workflow_definitions
           WHERE tenant_id=%s AND id=%s FOR UPDATE""",
        (tenant_id, workflow_id),
    ).fetchone()
    if not workflow:
        return None
    if workflow["status"] != "active":
        raise WorkflowDisabled
    _validate_workflow_trigger(conn, tenant_id, trigger)

    version_number, version_id = workflow["current_version"] + 1, str(uuid.uuid4())
    conn.execute(
        """INSERT INTO workflow_versions
           (id,tenant_id,workflow_id,version_number,definition,definition_hash,idempotency_key,
            author_credential_fingerprint,author_user_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (version_id, tenant_id, workflow_id, version_number, Jsonb(_workflow_definition(title, steps, trigger)),
         fingerprint, idempotency_key, author_credential_fingerprint, author_user_id),
    )
    conn.execute(
        """UPDATE workflow_definitions SET title=%s,current_version=%s,updated_at=clock_timestamp()
           WHERE tenant_id=%s AND id=%s""",
        (title, version_number, tenant_id, workflow_id),
    )
    emit_event(conn, tenant_id, "workflow.version_published", request_id=request_id,
               data={"workflow_id": workflow_id, "workflow_version_id": version_id, "version": version_number})
    return {"id": version_id, "version": version_number, "created": True}


def list_workflow_definitions(conn: Connection, tenant_id: str) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT id,title,status,current_version,created_at,updated_at
           FROM workflow_definitions WHERE tenant_id=%s ORDER BY updated_at DESC LIMIT 100""",
        (tenant_id,),
    ).fetchall()


def get_workflow_definition(conn: Connection, tenant_id: str, workflow_id: str) -> dict[str, Any] | None:
    workflow = conn.execute(
        """SELECT id,title,status,current_version,created_at,updated_at
           FROM workflow_definitions WHERE tenant_id=%s AND id=%s""",
        (tenant_id, workflow_id),
    ).fetchone()
    if workflow:
        workflow["versions"] = conn.execute(
            """SELECT v.id,v.version_number,v.definition,v.definition_hash,v.author_user_id,
                      u.email AS author_email,v.published_at
               FROM workflow_versions v LEFT JOIN users u ON u.id=v.author_user_id
               WHERE v.tenant_id=%s AND v.workflow_id=%s ORDER BY v.version_number""",
            (tenant_id, workflow_id),
        ).fetchall()
    return workflow


def trigger_workflow_definition(
    conn: Connection,
    tenant_id: str,
    workflow_id: str,
    idempotency_key: str,
    request_id: str,
) -> dict[str, Any] | None:
    _lock_tenant(conn, tenant_id)
    existing = conn.execute(
        """SELECT r.id,r.status,r.workflow_version_id,v.workflow_id,v.version_number
           FROM workflow_runs r LEFT JOIN workflow_versions v
             ON v.tenant_id=r.tenant_id AND v.id=r.workflow_version_id
           WHERE r.tenant_id=%s AND r.idempotency_key=%s""",
        (tenant_id, idempotency_key),
    ).fetchone()
    if existing:
        if existing["workflow_id"] != workflow_id:
            raise IdempotencyConflict
        result = {"id": existing["id"], "status": existing["status"], "created": False,
                  "workflow_version_id": existing["workflow_version_id"]}
        if existing["version_number"] is not None:
            result["version"] = existing["version_number"]
        return result

    version = conn.execute(
        """SELECT d.title,d.status,v.id,v.version_number,v.definition,v.definition_hash
           FROM workflow_definitions d JOIN workflow_versions v
             ON v.tenant_id=d.tenant_id AND v.workflow_id=d.id AND v.version_number=d.current_version
           WHERE d.tenant_id=%s AND d.id=%s""",
        (tenant_id, workflow_id),
    ).fetchone()
    if not version:
        return None
    if version["status"] != "active":
        raise WorkflowDisabled
    result = create_workflow(
        conn, tenant_id, version["title"], version["definition"]["steps"], idempotency_key, request_id,
        workflow_version_id=version["id"], definition_hash=version["definition_hash"],
    )
    result["workflow_version_id"] = version["id"]
    result["version"] = version["version_number"]
    return result


def _schedule_fingerprint(workflow_id: str, name: str, interval_seconds: int) -> str:
    payload = {"workflow_id": workflow_id, "name": name, "interval_seconds": interval_seconds}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def workflow_schedule_idempotent_result(
    conn: Connection, tenant_id: str, workflow_id: str, name: str,
    interval_seconds: int, idempotency_key: str,
) -> dict[str, Any] | None:
    fingerprint = _schedule_fingerprint(workflow_id, name, interval_seconds)
    existing = conn.execute(
        "SELECT id,fingerprint FROM workflow_schedules WHERE tenant_id=%s AND idempotency_key=%s",
        (tenant_id, idempotency_key),
    ).fetchone()
    if not existing:
        return None
    if existing["fingerprint"] != fingerprint:
        raise IdempotencyConflict
    return {**get_workflow_schedule(conn, tenant_id, existing["id"]), "created": False}


def list_workflow_schedules(conn: Connection, tenant_id: str) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT s.id,s.workflow_id,s.name,s.interval_seconds,s.status,s.next_run_at,
                  s.last_run_at,s.last_run_id,r.status AS last_run_status,s.pause_reason,
                  s.created_at,s.updated_at
           FROM workflow_schedules s LEFT JOIN workflow_runs r
             ON r.tenant_id=s.tenant_id AND r.id=s.last_run_id
           WHERE s.tenant_id=%s ORDER BY s.created_at DESC LIMIT 100""",
        (tenant_id,),
    ).fetchall()


def get_workflow_schedule(conn: Connection, tenant_id: str, schedule_id: str) -> dict[str, Any] | None:
    return conn.execute(
        """SELECT s.id,s.workflow_id,s.name,s.interval_seconds,s.status,s.next_run_at,
                  s.last_run_at,s.last_run_id,r.status AS last_run_status,s.pause_reason,
                  s.created_at,s.updated_at
           FROM workflow_schedules s LEFT JOIN workflow_runs r
             ON r.tenant_id=s.tenant_id AND r.id=s.last_run_id
           WHERE s.tenant_id=%s AND s.id=%s""",
        (tenant_id, schedule_id),
    ).fetchone()


def create_workflow_schedule(
    conn: Connection, tenant_id: str, workflow_id: str, name: str, interval_seconds: int,
    idempotency_key: str, request_id: str, created_by: str | None = None,
) -> dict[str, Any] | None:
    _lock_tenant(conn, tenant_id)
    existing = workflow_schedule_idempotent_result(
        conn, tenant_id, workflow_id, name, interval_seconds, idempotency_key,
    )
    if existing:
        return existing
    active_count = conn.execute(
        "SELECT count(*) AS count FROM workflow_schedules WHERE tenant_id=%s AND status IN ('active','paused')",
        (tenant_id,),
    ).fetchone()["count"]
    if active_count >= MAX_SCHEDULES_PER_TENANT:
        raise ScheduleLimitReached
    admit_rate_limit(conn, tenant_id)
    workflow = conn.execute(
        """SELECT d.status,v.definition FROM workflow_definitions d JOIN workflow_versions v
             ON v.tenant_id=d.tenant_id AND v.workflow_id=d.id AND v.version_number=d.current_version
           WHERE d.tenant_id=%s AND d.id=%s""",
        (tenant_id, workflow_id),
    ).fetchone()
    if not workflow:
        return None
    if workflow["status"] != "active":
        raise WorkflowDisabled
    if any(has_event_references(step["payload"].get("body")) for step in workflow["definition"]["steps"]):
        raise ValueError("Workflows that require webhook event data cannot be scheduled.")

    schedule_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO workflow_schedules
           (id,tenant_id,workflow_id,name,interval_seconds,next_run_at,idempotency_key,
            fingerprint,created_by)
           VALUES (%s,%s,%s,%s,%s,clock_timestamp()+(%s * interval '1 second'),%s,%s,%s)""",
        (schedule_id, tenant_id, workflow_id, name, interval_seconds, interval_seconds,
         idempotency_key, _schedule_fingerprint(workflow_id, name, interval_seconds), created_by),
    )
    emit_event(conn, tenant_id, "schedule.created", request_id=request_id,
               data={"schedule_id": schedule_id, "workflow_id": workflow_id,
                     "interval_seconds": interval_seconds, "actor_user_id": created_by})
    return {**get_workflow_schedule(conn, tenant_id, schedule_id), "created": True}


def set_workflow_schedule_status(
    conn: Connection, tenant_id: str, schedule_id: str, status: str, request_id: str,
    actor_user_id: str | None = None,
) -> dict[str, Any] | None:
    schedule = conn.execute(
        "SELECT status,interval_seconds FROM workflow_schedules WHERE tenant_id=%s AND id=%s FOR UPDATE",
        (tenant_id, schedule_id),
    ).fetchone()
    if not schedule:
        return None
    if schedule["status"] == "cancelled" and status != "cancelled":
        raise WorkflowScheduleCancelled
    if schedule["status"] == status:
        return get_workflow_schedule(conn, tenant_id, schedule_id)

    admit_rate_limit(conn, tenant_id)

    if status == "active":
        conn.execute(
            """UPDATE workflow_schedules SET status='active',pause_reason=NULL,
                 next_run_at=clock_timestamp()+(interval_seconds * interval '1 second'),
                 updated_at=clock_timestamp() WHERE tenant_id=%s AND id=%s""",
            (tenant_id, schedule_id),
        )
        kind = "schedule.resumed"
    elif status == "paused":
        conn.execute(
            """UPDATE workflow_schedules SET status='paused',pause_reason='user_paused',
                 updated_at=clock_timestamp() WHERE tenant_id=%s AND id=%s""",
            (tenant_id, schedule_id),
        )
        kind = "schedule.paused"
    else:
        conn.execute(
            """UPDATE workflow_schedules SET status='cancelled',pause_reason=NULL,
                 updated_at=clock_timestamp() WHERE tenant_id=%s AND id=%s""",
            (tenant_id, schedule_id),
        )
        kind = "schedule.cancelled"
    emit_event(conn, tenant_id, kind, request_id=request_id,
               data={"schedule_id": schedule_id, "actor_user_id": actor_user_id})
    return get_workflow_schedule(conn, tenant_id, schedule_id)


def dispatch_due_schedules(conn: Connection, batch_size: int = 20) -> int:
    dispatched = 0
    blocked_tenants: set[str] = set()
    for _ in range(batch_size):
        with conn.transaction():
            schedule = conn.execute(
                """SELECT id,tenant_id,workflow_id,interval_seconds,next_run_at,
                          to_char(next_run_at AT TIME ZONE 'UTC','YYYYMMDDHH24MISSUS') AS occurrence
                   FROM workflow_schedules WHERE status='active' AND next_run_at<=clock_timestamp()
                     AND tenant_id <> ALL(%s::text[])
                   ORDER BY next_run_at,id FOR UPDATE SKIP LOCKED LIMIT 1""",
                (sorted(blocked_tenants),),
            ).fetchone()
            if not schedule:
                return dispatched
            try:
                with conn.transaction():
                    _lock_tenant(conn, schedule["tenant_id"])
                    version = conn.execute(
                        """SELECT d.status,v.definition FROM workflow_definitions d JOIN workflow_versions v
                             ON v.tenant_id=d.tenant_id AND v.workflow_id=d.id AND v.version_number=d.current_version
                           WHERE d.tenant_id=%s AND d.id=%s""",
                        (schedule["tenant_id"], schedule["workflow_id"]),
                    ).fetchone()
                    reason = None
                    if not version:
                        reason = "workflow_missing"
                    elif version["status"] != "active":
                        reason = "workflow_disabled"
                    elif any(has_event_references(step["payload"].get("body"))
                             for step in version["definition"]["steps"]):
                        reason = "workflow_requires_webhook_event"
                    if reason:
                        conn.execute(
                            """UPDATE workflow_schedules SET status='paused',pause_reason=%s,
                                 updated_at=clock_timestamp() WHERE tenant_id=%s AND id=%s""",
                            (reason, schedule["tenant_id"], schedule["id"]),
                        )
                        emit_event(conn, schedule["tenant_id"], "schedule.paused",
                                   request_id=f"schedule:{schedule['id']}:{schedule['occurrence']}",
                                   data={"schedule_id": schedule["id"], "reason": reason})
                    else:
                        idempotency_key = f"schedule:{schedule['id']}:{schedule['occurrence']}"
                        run = trigger_workflow_definition(
                            conn, schedule["tenant_id"], schedule["workflow_id"], idempotency_key,
                            idempotency_key,
                        )
                        if run is None:
                            conn.execute(
                                """UPDATE workflow_schedules SET status='paused',pause_reason='workflow_missing',
                                     updated_at=clock_timestamp() WHERE tenant_id=%s AND id=%s""",
                                (schedule["tenant_id"], schedule["id"]),
                            )
                            emit_event(conn, schedule["tenant_id"], "schedule.paused",
                                       request_id=idempotency_key,
                                       data={"schedule_id": schedule["id"], "reason": "workflow_missing"})
                        else:
                            conn.execute(
                                """UPDATE workflow_schedules SET next_run_at=clock_timestamp()+
                                     (interval_seconds * interval '1 second'),last_run_at=clock_timestamp(),
                                     last_run_id=%s,updated_at=clock_timestamp()
                                   WHERE tenant_id=%s AND id=%s""",
                                (run["id"], schedule["tenant_id"], schedule["id"]),
                            )
                            emit_event(conn, schedule["tenant_id"], "schedule.dispatched",
                                       run_id=run["id"], request_id=idempotency_key,
                                       data={"schedule_id": schedule["id"],
                                             "workflow_version_id": run.get("workflow_version_id"),
                                             "version": run.get("version")})
                            dispatched += 1
            except (QueueFull, RateLimited):
                blocked_tenants.add(schedule["tenant_id"])
    return dispatched


def create_workflow(
    conn: Connection,
    tenant_id: str,
    title: str,
    steps: list[dict[str, Any]],
    idempotency_key: str,
    request_id: str,
    *,
    workflow_version_id: str | None = None,
    definition_hash: str | None = None,
    trigger_event_id: str | None = None,
) -> dict[str, Any]:
    fingerprint_payload = {"title": title, "steps": steps}
    if workflow_version_id is not None:
        fingerprint_payload["workflow_version_id"] = workflow_version_id
    if trigger_event_id is not None:
        fingerprint_payload["trigger_event_id"] = trigger_event_id
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    existing = conn.execute(
        "SELECT id,fingerprint,status FROM workflow_runs WHERE tenant_id=%s AND idempotency_key=%s",
        (tenant_id, idempotency_key),
    ).fetchone()
    if existing:
        if existing["fingerprint"] != fingerprint:
            raise IdempotencyConflict
        return {"id": existing["id"], "status": existing["status"], "created": False}

    # Serialize the lookup, capacity check, and insert within this tenant's transaction.
    _lock_tenant(conn, tenant_id)
    existing = conn.execute(
        "SELECT id,fingerprint,status FROM workflow_runs WHERE tenant_id=%s AND idempotency_key=%s",
        (tenant_id, idempotency_key),
    ).fetchone()
    if existing:
        if existing["fingerprint"] != fingerprint:
            raise IdempotencyConflict
        return {"id": existing["id"], "status": existing["status"], "created": False}

    _admit(conn, tenant_id)
    run_id, task_id = str(uuid.uuid4()), str(uuid.uuid4())
    definition = {"title": title, "steps": steps}
    conn.execute(
        """INSERT INTO workflow_runs
           (id,tenant_id,title,definition,status,idempotency_key,fingerprint,workflow_version_id,definition_hash,
            trigger_event_id)
           VALUES (%s,%s,%s,%s,'queued',%s,%s,%s,%s,%s)""",
        (run_id, tenant_id, title, Jsonb(definition), idempotency_key, fingerprint,
         workflow_version_id, definition_hash or _definition_fingerprint(title, steps), trigger_event_id),
    )
    conn.execute(
        "INSERT INTO tasks(id,tenant_id,run_id,max_attempts,request_id,traceparent) VALUES (%s,%s,%s,%s,%s,%s)",
        (task_id, tenant_id, run_id, MAX_ATTEMPTS, request_id, current_traceparent()),
    )
    emit_event(conn, tenant_id, "workflow.created", run_id=run_id, task_id=task_id,
               request_id=request_id, data={"title": title, "steps": len(steps),
                                            "workflow_version_id": workflow_version_id})
    return {"id": run_id, "task_id": task_id, "status": "queued", "created": True,
            "workflow_version_id": workflow_version_id}


def ingest_business_event(
    conn: Connection,
    tenant_id: str,
    event_key: str,
    payload: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    inserted = conn.execute(
        """INSERT INTO business_events(tenant_id,event_key,payload)
           VALUES (%s,%s,%s) ON CONFLICT DO NOTHING RETURNING event_key""",
        (tenant_id, event_key, Jsonb(payload)),
    ).fetchone()
    if inserted:
        run = create_workflow(
            conn,
            tenant_id,
            "Duplicate-safe order payment",
            [
                {"name": "reserve inventory", "action": "record", "payload": {"order": payload.get("order", "demo-order")}},
                {"name": "charge payment", "action": "charge", "payload": {"amount": payload.get("amount", 25), "currency": payload.get("currency", "USD")}},
                {"name": "confirm shipment", "action": "record", "payload": {"order": payload.get("order", "demo-order")}},
            ],
            f"business-event:{event_key}",
            request_id,
        )
        conn.execute(
            "UPDATE business_events SET run_id=%s WHERE tenant_id=%s AND event_key=%s",
            (run["id"], tenant_id, event_key),
        )
        received_count, created = 1, run["created"]
    else:
        row = conn.execute(
            """UPDATE business_events SET received_count=received_count+1,
                      last_received_at=clock_timestamp()
               WHERE tenant_id=%s AND event_key=%s RETURNING received_count,run_id,payload""",
            (tenant_id, event_key),
        ).fetchone()
        if row["payload"] != payload:
            raise IdempotencyConflict
        received_count, created = row["received_count"], False
        run = {"id": row["run_id"]}

    emit_event(conn, tenant_id, "business_event.received", run_id=run["id"], request_id=request_id,
               data={"event_key": event_key, "received_count": received_count, "workflow_created": created})
    return {"event_key": event_key, "received": received_count, "workflow_id": run["id"], "created": created}


def claim_task(conn: Connection, worker_id: str, lease_seconds: float,
               *, tenant_id: str | None = None) -> dict[str, Any] | None:
    with conn.transaction():
        tenant_filter = "AND t.tenant_id=%s" if tenant_id else ""
        selection = """SELECT t.id,t.tenant_id,t.run_id,t.step_index,t.attempts,t.max_attempts,
                          t.last_worker,t.request_id,t.traceparent,w.title,w.definition,i.payload AS trigger_payload
                   FROM tasks t JOIN workflow_runs w ON w.id=t.run_id
                   LEFT JOIN incoming_events i ON i.id=w.trigger_event_id AND i.workspace_id=w.tenant_id
                   WHERE t.status IN ('queued','retry_wait') AND t.available_at<=clock_timestamp()
                     AND t.last_worker IS DISTINCT FROM %s
                     AND w.status NOT IN ('cancelled','completed','failed')
                     {tenant_filter}
                   ORDER BY t.available_at,t.created_at
                   FOR UPDATE OF t SKIP LOCKED LIMIT 1"""
        selection = selection.format(tenant_filter=tenant_filter)
        parameters = (worker_id, tenant_id) if tenant_id else (worker_id,)
        task = conn.execute(selection, parameters).fetchone()
        if not task:
            # If this worker is the only available capacity, allow it to continue its prior run.
            fallback_parameters = (tenant_id,) if tenant_id else ()
            task = conn.execute(selection.replace("AND t.last_worker IS DISTINCT FROM %s", ""),
                                fallback_parameters).fetchone()
        if not task:
            return None
        conn.execute(
            """UPDATE tasks SET status='running',attempts=attempts+1,lease_owner=%s,
                      lease_until=clock_timestamp()+(%s * interval '1 second'),last_worker=%s,
                      updated_at=clock_timestamp() WHERE id=%s""",
            (worker_id, lease_seconds, worker_id, task["id"]),
        )
        conn.execute(
            "UPDATE workflow_runs SET status='running' WHERE id=%s AND status IN ('queued','retrying')",
            (task["run_id"],),
        )
        emit_event(conn, task["tenant_id"], "task.claimed", run_id=task["run_id"], task_id=task["id"],
                   worker_id=worker_id, request_id=task["request_id"],
                   data={"attempt": task["attempts"] + 1, "step_index": task["step_index"]})
        task["attempts"] += 1
        return task


def heartbeat(conn: Connection, worker_id: str, task_id: str | None, lease_seconds: float) -> None:
    with conn.transaction():
        conn.execute(
            """INSERT INTO workers(id,pid,host) VALUES (%s,%s,%s)
               ON CONFLICT(id) DO UPDATE SET pid=EXCLUDED.pid,host=EXCLUDED.host,
                 heartbeat_at=clock_timestamp(),stopped_at=NULL""",
            (worker_id, os.getpid(), socket.gethostname()),
        )
        if task_id:
            conn.execute(
                """UPDATE tasks SET lease_until=clock_timestamp()+(%s * interval '1 second'),
                          updated_at=clock_timestamp()
                   WHERE id=%s AND status='running' AND lease_owner=%s""",
                (lease_seconds, task_id, worker_id),
            )


def mark_worker_stopped(conn: Connection, worker_id: str) -> None:
    conn.execute("UPDATE workers SET stopped_at=clock_timestamp() WHERE id=%s", (worker_id,))


def recover_expired_leases(conn: Connection, request_id: str = "coordinator") -> int:
    recovered = 0
    with conn.transaction():
        expired = conn.execute(
            """SELECT t.id,t.tenant_id,t.run_id,t.attempts,t.max_attempts,t.last_worker,t.request_id,
                      w.status AS run_status
               FROM tasks t JOIN workflow_runs w ON w.id=t.run_id
               WHERE t.status='running' AND t.lease_until<clock_timestamp()
               FOR UPDATE OF t SKIP LOCKED"""
        ).fetchall()
        for task in expired:
            recovered += 1
            if task["run_status"] == "cancelled":
                conn.execute(
                    """UPDATE tasks SET status='cancelled',lease_owner=NULL,lease_until=NULL,
                              updated_at=clock_timestamp() WHERE id=%s""", (task["id"],)
                )
                emit_event(conn, task["tenant_id"], "task.cancelled_after_lease_expiry", run_id=task["run_id"],
                           worker_id=task["last_worker"], request_id=task["request_id"])
                continue
            if task["attempts"] >= task["max_attempts"]:
                error = {"kind": "lease_expired", "detail": "Worker lease expired; retry budget exhausted."}
                conn.execute(
                    """UPDATE tasks SET status='dead',lease_owner=NULL,lease_until=NULL,last_error=%s,
                       updated_at=clock_timestamp() WHERE id=%s""",
                    (Jsonb(error), task["id"]),
                )
                conn.execute("UPDATE workflow_runs SET status='failed',finished_at=clock_timestamp() WHERE id=%s",
                             (task["run_id"],))
                conn.execute(
                    "INSERT INTO dead_letters(tenant_id,run_id,task_id,attempts,error) VALUES (%s,%s,%s,%s,%s)",
                    (task["tenant_id"], task["run_id"], task["id"], task["attempts"], Jsonb(error)),
                )
                kind = "task.dead_lettered"
            else:
                delay = min(30, 0.25 * (2 ** max(0, task["attempts"] - 1)))
                conn.execute(
                    """UPDATE tasks SET status='retry_wait',available_at=clock_timestamp()+(%s * interval '1 second'),
                              lease_owner=NULL,lease_until=NULL,last_error=%s,updated_at=clock_timestamp()
                       WHERE id=%s""",
                    (delay, Jsonb({"kind": "lease_expired", "detail": "Worker lease expired."}), task["id"]),
                )
                conn.execute("UPDATE workflow_runs SET status='retrying' WHERE id=%s", (task["run_id"],))
                kind = "task.lease_expired"
            emit_event(conn, task["tenant_id"], kind, run_id=task["run_id"], task_id=task["id"],
                       worker_id=task["last_worker"], request_id=task["request_id"],
                       data={"attempts": task["attempts"], "max_attempts": task["max_attempts"]})
    return recovered


def fail_task(conn: Connection, task: dict[str, Any], worker_id: str, error: Exception, request_id: str) -> None:
    data = {"kind": type(error).__name__, "detail": str(error)[:500]}
    with conn.transaction():
        row = conn.execute(
            """SELECT t.tenant_id,t.run_id,t.attempts,t.max_attempts,w.status AS run_status
               FROM tasks t JOIN workflow_runs w ON w.id=t.run_id
               WHERE t.id=%s AND t.lease_owner=%s FOR UPDATE OF t,w""",
            (task["id"], worker_id),
        ).fetchone()
        if not row:
            return
        if row["run_status"] == "cancelled":
            conn.execute("""UPDATE tasks SET status='cancelled',lease_owner=NULL,lease_until=NULL,
                          updated_at=clock_timestamp() WHERE id=%s""", (task["id"],))
            emit_event(conn, row["tenant_id"], "task.cancelled_after_action_failure", run_id=row["run_id"],
                       task_id=task["id"], worker_id=worker_id, request_id=request_id)
            return
        if row["attempts"] >= row["max_attempts"] or isinstance(error, PermanentActionError):
            conn.execute(
                """UPDATE tasks SET status='dead',lease_owner=NULL,lease_until=NULL,last_error=%s,
                   updated_at=clock_timestamp() WHERE id=%s""",
                (Jsonb(data), task["id"]),
            )
            conn.execute("UPDATE workflow_runs SET status='failed',finished_at=clock_timestamp() WHERE id=%s",
                         (task["run_id"],))
            conn.execute(
                "INSERT INTO dead_letters(tenant_id,run_id,task_id,attempts,error) VALUES (%s,%s,%s,%s,%s)",
                (row["tenant_id"], row["run_id"], task["id"], row["attempts"], Jsonb(data)),
            )
            kind = "task.dead_lettered"
        else:
            delay = min(30, 0.25 * (2 ** max(0, row["attempts"] - 1)))
            if isinstance(error, RetryableActionError) and error.retry_after is not None:
                delay = max(delay, error.retry_after)
            conn.execute(
                """UPDATE tasks SET status='retry_wait',available_at=clock_timestamp()+(%s * interval '1 second'),
                          lease_owner=NULL,lease_until=NULL,last_error=%s,updated_at=clock_timestamp()
                   WHERE id=%s""",
                (delay, Jsonb(data), task["id"]),
            )
            conn.execute("UPDATE workflow_runs SET status='retrying' WHERE id=%s", (task["run_id"],))
            kind = "task.retry_scheduled"
        emit_event(conn, row["tenant_id"], kind, run_id=row["run_id"], task_id=task["id"],
                   worker_id=worker_id, request_id=request_id,
                   data={**data, "attempts": row["attempts"], "max_attempts": row["max_attempts"]})


def execute_step(conn: Connection, worker_id: str, task: dict[str, Any], request_id: str) -> None:
    definition = task["definition"]
    steps = definition["steps"]
    index = task["step_index"]
    if index >= len(steps):
        with conn.transaction():
            conn.execute("UPDATE tasks SET status='succeeded',lease_owner=NULL,lease_until=NULL WHERE id=%s",
                         (task["id"],))
            conn.execute("UPDATE workflow_runs SET status='completed',finished_at=clock_timestamp() WHERE id=%s",
                         (task["run_id"],))
            emit_event(conn, task["tenant_id"], "workflow.completed", run_id=task["run_id"],
                       task_id=task["id"], worker_id=worker_id, request_id=request_id)
        return

    step = steps[index]
    action, payload = step["action"], step["payload"]
    external_actions = {"http", "slack_message"}
    if (DEMO_MODE and action in external_actions) or (not DEMO_MODE and action not in external_actions):
        raise PermanentActionError("Workflow action is not allowed in the current execution mode.")
    if action in external_actions:
        from app.settings import LEASE_SECONDS, secret_encryption_key

        active = conn.execute(
            """SELECT w.status FROM tasks t JOIN workflow_runs w ON w.id=t.run_id
               WHERE t.id=%s AND t.status='running' AND t.lease_owner=%s
                 AND t.lease_until>clock_timestamp()""",
            (task["id"], worker_id),
        ).fetchone()
        if not active:
            return
        if active["status"] == "cancelled":
            with conn.transaction():
                conn.execute("""UPDATE tasks SET status='cancelled',lease_owner=NULL,lease_until=NULL,
                              updated_at=clock_timestamp() WHERE id=%s AND lease_owner=%s""",
                             (task["id"], worker_id))
                kind = "task.cancelled_before_http_action" if action == "http" else "task.cancelled_before_slack_action"
                emit_event(conn, task["tenant_id"], kind, run_id=task["run_id"],
                           task_id=task["id"], worker_id=worker_id, request_id=request_id)
            return
        heartbeat(conn, worker_id, task["id"], LEASE_SECONDS)
        key = secret_encryption_key(required=True)
        if action == "http":
            from app.http_action import execute_http_action

            credential = workspace_credential_secret(conn, task["tenant_id"], payload["credential_id"], key)
            if not credential or credential["provider"] != "http":
                raise PermanentActionError("HTTP action credential is unavailable or has the wrong provider.")
            result = execute_http_action(payload, credential["secret"], f"{task['run_id']}:{index}",
                                         credential["allowed_host"], task.get("trigger_payload"))
        else:
            from app.slack import post_message

            credential_id = slack_action_credential_id(conn, task["tenant_id"])
            credential = (workspace_credential_secret(conn, task["tenant_id"], credential_id, key)
                          if credential_id else None)
            if not credential or credential["provider"] != "slack":
                raise PermanentActionError("Slack App credential is unavailable.")
            result = post_message(payload, credential["secret"],
                                  timeout_seconds=min(1.0, LEASE_SECONDS / 2))
        heartbeat(conn, worker_id, task["id"], LEASE_SECONDS)
    else:
        from app.sandbox import execute_sandbox_action

        result = execute_sandbox_action(conn, worker_id, task, step)
        if result is None:
            return
    with conn.transaction():
        locked = conn.execute(
            """SELECT t.tenant_id,t.run_id,t.step_index,w.definition,w.status
               FROM tasks t JOIN workflow_runs w ON w.id=t.run_id
               WHERE t.id=%s AND t.lease_owner=%s AND t.lease_until>clock_timestamp()
                 AND t.status='running' FOR UPDATE OF t,w""",
            (task["id"], worker_id),
        ).fetchone()
        if not locked:
            return
        if locked["step_index"] != index:
            return
        effect_key = f"{task['run_id']}:{index}"
        if locked["status"] == "cancelled":
            if action in external_actions:
                created = conn.execute(
                    """INSERT INTO side_effects(id,tenant_id,run_id,idempotency_key,step_index,result)
                       VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,idempotency_key) DO NOTHING
                       RETURNING id""",
                    (str(uuid.uuid4()), task["tenant_id"], task["run_id"], effect_key, index, Jsonb(result)),
                ).fetchone()
                emit_event(conn, task["tenant_id"], "side_effect.completed_after_cancel", run_id=task["run_id"],
                           task_id=task["id"], worker_id=worker_id, request_id=request_id,
                           data={"step_index": index, "recorded": bool(created)})
            conn.execute("""UPDATE tasks SET status='cancelled',lease_owner=NULL,lease_until=NULL,
                          updated_at=clock_timestamp() WHERE id=%s""", (task["id"],))
            return
        created = conn.execute(
            """INSERT INTO side_effects(id,tenant_id,run_id,idempotency_key,step_index,result)
               VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,idempotency_key) DO NOTHING
               RETURNING id""",
            (str(uuid.uuid4()), task["tenant_id"], task["run_id"], effect_key, index, Jsonb(result)),
        ).fetchone()
        emit_event(conn, task["tenant_id"], "side_effect.applied" if created else "side_effect.deduplicated",
                   run_id=task["run_id"], task_id=task["id"], worker_id=worker_id, request_id=request_id,
                   data={"step_index": index, "idempotency_key": effect_key})
        next_index = index + 1
        if next_index == len(locked["definition"]["steps"]):
            conn.execute(
                """UPDATE tasks SET status='succeeded',step_index=%s,attempts=0,lease_owner=NULL,
                          lease_until=NULL,updated_at=clock_timestamp() WHERE id=%s""",
                (next_index, task["id"]),
            )
            conn.execute("UPDATE workflow_runs SET status='completed',finished_at=clock_timestamp() WHERE id=%s",
                         (task["run_id"],))
            kind = "workflow.completed"
        else:
            conn.execute(
                """UPDATE tasks SET status='queued',step_index=%s,attempts=0,lease_owner=NULL,
                          lease_until=NULL,available_at=clock_timestamp(),updated_at=clock_timestamp()
                   WHERE id=%s""",
                (next_index, task["id"]),
            )
            kind = "task.step_completed"
        emit_event(conn, task["tenant_id"], kind, run_id=task["run_id"], task_id=task["id"],
                   worker_id=worker_id, request_id=request_id,
                   data={"step_index": index, "step_name": step["name"]})


def run_summary(conn: Connection, tenant_id: str, run_id: str) -> dict[str, Any] | None:
    run = conn.execute(
        """SELECT w.id,w.title,w.status,w.definition,w.workflow_version_id,w.definition_hash,
                  w.trigger_event_id,i.event_key AS trigger_event_key,
                  i.payload_sha256 AS trigger_event_payload_sha256,
                  i.event_type AS trigger_event_type,
                  i.received_at AS trigger_event_received_at,
                  w.created_at,w.finished_at,
                  t.id AS task_id,t.status AS task_status,t.step_index,t.attempts,t.max_attempts,
                  t.lease_owner,t.lease_until,t.last_error
           FROM workflow_runs w JOIN tasks t ON t.run_id=w.id
           LEFT JOIN incoming_events i ON i.id=w.trigger_event_id AND i.workspace_id=w.tenant_id
           WHERE w.tenant_id=%s AND w.id=%s""",
        (tenant_id, run_id),
    ).fetchone()
    if not run:
        return None
    if run["trigger_event_id"]:
        run["trigger_event"] = {
            "id": run.pop("trigger_event_id"),
            "event_key": run.pop("trigger_event_key"),
            "payload_sha256": run.pop("trigger_event_payload_sha256"),
            "type": run.pop("trigger_event_type"),
            "received_at": run.pop("trigger_event_received_at"),
        }
    else:
        for key in ("trigger_event_id", "trigger_event_key", "trigger_event_payload_sha256",
                    "trigger_event_type", "trigger_event_received_at"):
            run.pop(key)
    run["events"] = conn.execute(
        "SELECT sequence,kind,worker_id,request_id,data,created_at FROM events WHERE tenant_id=%s AND run_id=%s ORDER BY sequence",
        (tenant_id, run_id),
    ).fetchall()
    run["side_effects"] = conn.execute(
        "SELECT idempotency_key,step_index,result,created_at FROM side_effects WHERE tenant_id=%s AND run_id=%s ORDER BY step_index",
        (tenant_id, run_id),
    ).fetchall()
    return run


def dashboard(conn: Connection, tenant_id: str, *, include_workers: bool = False) -> dict[str, Any]:
    counts = conn.execute(
        """SELECT count(*) FILTER (WHERE status IN ('queued','retry_wait')) AS queue_depth,
                  count(*) FILTER (WHERE status='running') AS running,
                  count(*) FILTER (WHERE status='completed') AS completed,
                  count(*) FILTER (WHERE status='failed') AS failed
           FROM workflow_runs WHERE tenant_id=%s""", (tenant_id,)
    ).fetchone()
    queue = conn.execute(
        "SELECT count(*) AS count FROM tasks WHERE tenant_id=%s AND status IN ('queued','retry_wait','running')",
        (tenant_id,),
    ).fetchone()["count"]
    events = conn.execute(
        """SELECT count(*) AS received_count,coalesce(sum(received_count-1),0) AS duplicate_count
           FROM business_events WHERE tenant_id=%s""", (tenant_id,)
    ).fetchone()
    effects = conn.execute("SELECT count(*) AS count FROM side_effects WHERE tenant_id=%s", (tenant_id,)).fetchone()["count"]
    dlq = conn.execute("SELECT count(*) AS count FROM dead_letters WHERE tenant_id=%s AND replayed_at IS NULL", (tenant_id,)).fetchone()["count"]
    operations = conn.execute(
        """SELECT coalesce(extract(epoch FROM (clock_timestamp() - min(available_at) FILTER (
                          WHERE status IN ('queued','retry_wait') AND available_at<=clock_timestamp()
                      ))),0) AS oldest_ready_seconds,
                  count(*) FILTER (WHERE status='running' AND lease_until<=clock_timestamp()) AS expired_leases
           FROM tasks WHERE tenant_id=%s""", (tenant_id,)
    ).fetchone()
    workers = []
    if include_workers:
        workers = conn.execute(
            """SELECT id,pid,host,started_at,heartbeat_at,stopped_at,
                      heartbeat_at>clock_timestamp()-interval '3 seconds' AND stopped_at IS NULL AS alive
               FROM workers ORDER BY id"""
        ).fetchall()
    workflows = conn.execute(
        """SELECT w.id,w.title,w.status,w.created_at,t.step_index,t.attempts,t.last_worker,t.lease_owner,t.last_error
           FROM workflow_runs w JOIN tasks t ON t.run_id=w.id
           WHERE w.tenant_id=%s ORDER BY w.created_at DESC LIMIT 30""", (tenant_id,)
    ).fetchall()
    latencies = conn.execute(
        """SELECT percentile_cont(0.50) WITHIN GROUP (ORDER BY extract(epoch FROM (finished_at-created_at))*1000) AS p50,
                  percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM (finished_at-created_at))*1000) AS p95,
                  percentile_cont(0.99) WITHIN GROUP (ORDER BY extract(epoch FROM (finished_at-created_at))*1000) AS p99
           FROM workflow_runs WHERE tenant_id=%s AND finished_at IS NOT NULL""", (tenant_id,)
    ).fetchone()
    return {"counts": counts, "queue_depth": queue, "events": events, "side_effect_count": effects,
            "operations": operations,
            "dlq_size": dlq, "workers": workers, "workflows": workflows, "latency_ms": latencies}

