from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import uuid

import pytest
from psycopg import connect
from psycopg.errors import RaiseException
from psycopg.rows import dict_row
from fastapi import HTTPException
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import get_current_span

from app.settings import DATABASE_URL
from app.telemetry import trace_context
from app.auth import SESSION_COOKIE, verified_oidc_profile
from app.main import event_cursor, same_origin
from app.store import (
    QueueFull,
    claim_task,
    create_auth_session,
    create_workflow,
    dashboard,
    dispatch_due_schedules,
    emit_event,
    ingest_business_event,
    migrate,
    set_workspace_member,
    upsert_oidc_user,
)
from tests.conftest import (ADMIN, OTHER, OTHER_TENANT, TEST_TENANT, VIEWER, drive_run, get_run, make_workflow,
                            random_tenant)


@pytest.mark.parametrize(("name", "value"), [
    ("RELAYCORE_WORKERS", "-1"),
    ("RELAYCORE_MAX_ATTEMPTS", "0"),
    ("RELAYCORE_MAX_ATTEMPTS", "11"),
    ("RELAYCORE_QUEUE_LIMIT", "0"),
    ("RELAYCORE_SCHEDULE_LIMIT", "0"),
    ("RELAYCORE_RATE_LIMIT_PER_MINUTE", "0"),
    ("RELAYCORE_WEBHOOK_PAYLOAD_RETENTION_DAYS", "0"),
    ("RELAYCORE_LEASE_SECONDS", "0"),
    ("RELAYCORE_LEASE_SECONDS", "0.1"),
    ("RELAYCORE_LEASE_SECONDS", "nan"),
])
def test_invalid_runtime_limits_fail_during_settings_import(name, value):
    environment = os.environ.copy()
    environment[name] = value
    root = os.path.dirname(os.path.dirname(__file__))
    result = subprocess.run([sys.executable, "-c", "import app.settings"], cwd=root, env=environment,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert name in result.stderr


def test_auth_roles_tenant_boundary_and_request_correlation(client):
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers=VIEWER).status_code == 200
    assert make_workflow(client, tenant_headers=VIEWER).status_code == 403
    response = make_workflow(client)
    assert response.status_code == 202
    run_id = response.json()["id"]
    assert client.get(f"/api/workflows/{run_id}", headers=OTHER).status_code == 404
    correlated = client.get("/api/status", headers={**ADMIN, "X-Request-ID": "build-test-001"})
    assert correlated.headers["X-Request-ID"] == "build-test-001"
    assert client.get("/metrics").status_code == 401
    metrics = client.get("/metrics", headers=ADMIN)
    assert metrics.status_code == 200
    assert "relaycore_queue_oldest_ready_seconds" in metrics.text
    assert "relaycore_expired_leases" in metrics.text


def test_workspace_metrics_tokens_are_hash_only_read_only_and_revocable(client):
    owner_email = "metrics-token-owner@example.test"
    viewer_email = "metrics-token-viewer@example.test"
    with client.app.state.pool.connection() as conn, conn.transaction():
        owner = upsert_oidc_user(conn, "https://identity.example", str(uuid.uuid4()), owner_email, owner_email)
        owner_session = create_auth_session(conn, owner["id"], "metrics-token-owner", 3600)
        viewer = upsert_oidc_user(conn, "https://identity.example", str(uuid.uuid4()), viewer_email, viewer_email)
        viewer_session = create_auth_session(conn, viewer["id"], "metrics-token-viewer", 3600)
    owner_headers = {"Cookie": f"{SESSION_COOKIE}={owner_session}"}
    workspace = client.post("/api/workspaces", headers=owner_headers, json={"name": "Metrics Team"})
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]
    path = f"/api/workspaces/{workspace_id}/metrics-tokens"
    scoped_owner = {**owner_headers, "X-Workspace-ID": workspace_id}
    with client.app.state.pool.connection() as conn, conn.transaction():
        set_workspace_member(conn, workspace_id, owner["id"], viewer["id"], "VIEWER", "metrics-token-member")
    scoped_viewer = {"Cookie": f"{SESSION_COOKIE}={viewer_session}", "X-Workspace-ID": workspace_id}
    denied = client.post(path, headers=scoped_viewer, json={"name": "viewer"})
    assert denied.status_code == 403

    created = client.post(path, headers=scoped_owner, json={"name": "Prometheus production"})
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    assert token.startswith("rcm_")
    assert "token" not in client.get(path, headers=scoped_owner).text
    with client.app.state.pool.connection() as conn:
        stored_hash = conn.execute(
            "SELECT token_hash FROM workspace_metrics_tokens WHERE id=%s", (created.json()["id"],)
        ).fetchone()["token_hash"]
    assert stored_hash == hashlib.sha256(token.encode()).hexdigest()

    bearer = {"Authorization": f"Bearer {token}"}
    scraped = client.get("/metrics", headers=bearer)
    assert scraped.status_code == 200 and "relaycore_queue_depth" in scraped.text
    assert client.get("/api/status", headers=bearer).status_code == 401
    assert client.get("/metrics", headers={**bearer, "X-Workspace-ID": str(uuid.uuid4())}).status_code == 404

    revoked = client.delete(f"{path}/{created.json()['id']}", headers=scoped_owner)
    assert revoked.status_code == 204
    assert client.get("/metrics", headers=bearer).status_code == 401


def test_prometheus_configuration_and_rules_are_valid_yaml():
    from pathlib import Path

    import yaml

    root = Path(__file__).parents[1]
    config = yaml.safe_load((root / "monitoring" / "prometheus.yml").read_text(encoding="utf-8"))
    rules = yaml.safe_load((root / "monitoring" / "relaycore.rules.yml").read_text(encoding="utf-8"))
    rule_tests = yaml.safe_load((root / "monitoring" / "relaycore.rules.test.yml").read_text(encoding="utf-8"))
    assert config["rule_files"] == ["/etc/prometheus/relaycore.rules.yml"]
    assert config["scrape_configs"][0]["authorization"]["credentials_file"].endswith(
        "relaycore-metrics-token"
    )
    alerts = rules["groups"][0]["rules"]
    assert {alert["alert"] for alert in alerts} == {
        "RelayCoreQueueAgeHigh", "RelayCoreExpiredLeases", "RelayCoreDeadLettersPresent",
    }
    assert all("expr" in alert and "for" in alert for alert in alerts)
    assert len(rule_tests["tests"]) == 2


def test_queue_operational_metrics_report_ready_age_and_tenant_scoped_expired_leases():
    tenant, other = random_tenant(), random_tenant()
    with pytest.raises(RuntimeError, match="rollback metric fixtures"):
        with connect(DATABASE_URL, row_factory=dict_row) as conn, conn.transaction():
            queued = create_workflow(
                conn, tenant, "metrics queued", [{"name": "record", "action": "record", "payload": {}}],
                f"metrics-queued:{uuid.uuid4()}", "metrics-test",
            )
            leased = create_workflow(
                conn, other, "metrics expired lease", [{"name": "record", "action": "record", "payload": {}}],
                f"metrics-lease:{uuid.uuid4()}", "metrics-test",
            )
            conn.execute(
                "UPDATE tasks SET available_at=clock_timestamp()-interval '1 hour' WHERE run_id=%s",
                (queued["id"],),
            )
            conn.execute(
                """UPDATE tasks SET status='running',lease_owner='metrics-test',
                          lease_until=clock_timestamp()-interval '5 seconds' WHERE run_id=%s""",
                (leased["id"],),
            )
            queue_metrics = dashboard(conn, tenant)["operations"]
            lease_metrics = dashboard(conn, other)["operations"]
            assert queue_metrics["oldest_ready_seconds"] >= 3600
            assert queue_metrics["expired_leases"] == 0
            assert lease_metrics["oldest_ready_seconds"] == 0
            assert lease_metrics["expired_leases"] == 1
            raise RuntimeError("rollback metric fixtures")


def test_trace_context_follows_durable_task_to_worker():
    tenant = random_tenant()
    provider = TracerProvider()
    tracer = provider.get_tracer("relaycore.test")
    try:
        with pytest.raises(RuntimeError, match="rollback trace fixtures"):
            with connect(DATABASE_URL, row_factory=dict_row) as conn, conn.transaction():
                with tracer.start_as_current_span("http.request") as parent:
                    created = create_workflow(
                        conn, tenant, "trace propagation", [{"name": "record", "action": "record", "payload": {}}],
                        f"trace-propagation:{uuid.uuid4()}", "trace-test-request",
                    )
                    parent_context = parent.get_span_context()
                task = claim_task(conn, "trace-test-worker", 1.2, tenant_id=tenant)
                assert task and task["run_id"] == created["id"]
                assert task["traceparent"]
                propagated = get_current_span(trace_context(task["traceparent"])).get_span_context()
                assert propagated.trace_id == parent_context.trace_id
                assert propagated.span_id == parent_context.span_id
                raise RuntimeError("rollback trace fixtures")
    finally:
        provider.shutdown()


def test_workspace_sessions_rbac_and_cross_workspace_isolation(client):
    def cookie(token: str, workspace_id: str | None = None, request_id: str | None = None) -> dict[str, str]:
        headers = {"Cookie": f"{SESSION_COOKIE}={token}"}
        if workspace_id:
            headers["X-Workspace-ID"] = workspace_id
        if request_id:
            headers["X-Request-ID"] = request_id
        return headers

    def signed_in(email: str):
        with client.app.state.pool.connection() as conn, conn.transaction():
            user = upsert_oidc_user(conn, "https://identity.example", str(uuid.uuid4()), email, email)
            token = create_auth_session(conn, user["id"], "workspace-auth-test", 3600)
        return user, token

    owner, owner_token = signed_in("owner@example.test")
    viewer, viewer_token = signed_in("viewer@example.test")
    admin, admin_token = signed_in("admin@example.test")
    outsider, outsider_token = signed_in("outsider@example.test")
    created = client.post("/api/workspaces", headers=cookie(owner_token, request_id="workspace-create-test"),
                          json={"name": "  Team Alpha  "})
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id"]
    assert created.json()["name"] == "Team Alpha" and created.json()["role"] == "OWNER"
    assert client.get("/api/workspaces", headers=cookie(owner_token)).json() == [
        {"id": workspace_id, "name": "Team Alpha", "role": "OWNER"}
    ]

    member = client.put(f"/api/workspaces/{workspace_id}/members", headers=cookie(owner_token, workspace_id),
                        json={"user_id": viewer["id"], "role": "VIEWER"})
    assert member.status_code == 200, member.text
    assert client.get("/api/status", headers=cookie(viewer_token, workspace_id)).status_code == 200
    body = {"title": "viewer write", "steps": [{"name": "record", "action": "record", "payload": {}}]}
    assert client.post("/api/workflows", headers=cookie(viewer_token, workspace_id), json=body).status_code == 403

    promoted = client.put(f"/api/workspaces/{workspace_id}/members", headers=cookie(owner_token, workspace_id),
                          json={"user_id": viewer["id"], "role": "DEVELOPER"})
    assert promoted.status_code == 200
    run_response = client.post("/api/workflows", headers=cookie(viewer_token, workspace_id), json=body)
    assert run_response.status_code == 202, run_response.text

    elevated = client.put(f"/api/workspaces/{workspace_id}/members", headers=cookie(owner_token, workspace_id),
                          json={"user_id": viewer["id"], "role": "ADMIN"})
    assert elevated.status_code == 200
    assert client.put(f"/api/workspaces/{workspace_id}/members", headers=cookie(owner_token, workspace_id),
                      json={"user_id": admin["id"], "role": "ADMIN"}).status_code == 200
    assert client.put(f"/api/workspaces/{workspace_id}/members", headers=cookie(admin_token, workspace_id),
                      json={"user_id": owner["id"], "role": "DEVELOPER"}).status_code == 403
    assert client.delete(f"/api/workspaces/{workspace_id}/members/{owner['id']}",
                         headers=cookie(admin_token, workspace_id)).status_code == 403
    self_promotion = client.put(f"/api/workspaces/{workspace_id}/members", headers=cookie(viewer_token, workspace_id),
                                json={"user_id": viewer["id"], "role": "OWNER"})
    assert self_promotion.status_code == 403
    assert client.delete(f"/api/workspaces/{workspace_id}/members/{owner['id']}",
                         headers=cookie(owner_token, workspace_id)).status_code == 409

    other_workspace = client.post("/api/workspaces", headers=cookie(outsider_token), json={"name": "Team Beta"})
    assert other_workspace.status_code == 201, other_workspace.text
    other_id = other_workspace.json()["id"]
    assert client.post("/api/workspaces", headers=cookie(owner_token), json={"name": "Team Gamma"}).status_code == 201
    assert client.get("/api/status", headers=cookie(owner_token)).status_code == 400
    assert client.get("/api/status", headers=cookie(owner_token, workspace_id)).status_code == 200
    assert client.get("/api/status", headers=cookie(outsider_token, workspace_id)).status_code == 404
    assert client.get(f"/api/workflows/{run_response.json()['id']}",
                      headers=cookie(outsider_token, other_id)).status_code == 404
    assert client.get(f"/api/workspaces/{other_id}/members",
                      headers=cookie(owner_token, workspace_id)).status_code == 404

    with client.app.state.pool.connection() as conn:
        fingerprint = hashlib.sha256(owner_token.encode()).hexdigest()
        assert conn.execute("SELECT 1 FROM auth_sessions WHERE token_hash=%s", (owner_token,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM auth_sessions WHERE token_hash=%s", (fingerprint,)).fetchone()
        assert conn.execute(
            "SELECT count(*) AS n FROM events WHERE tenant_id=%s AND kind LIKE 'workspace.%%'", (workspace_id,)
        ).fetchone()["n"] >= 3

    assert client.post("/auth/logout", headers=cookie(owner_token)).status_code == 204
    assert client.get("/api/me", headers=cookie(owner_token)).status_code == 401
    with client.app.state.pool.connection() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM identity_events WHERE user_id=%s AND kind IN ('auth.login','auth.logout')",
            (owner["id"],),
        ).fetchone()["n"] == 2


def test_session_expiry_and_origin_check_fail_closed(client):
    with client.app.state.pool.connection() as conn, conn.transaction():
        user = upsert_oidc_user(conn, "https://identity.example", str(uuid.uuid4()),
                                "expired@example.test", "Expired User")
        token = create_auth_session(conn, user["id"], "expiry-test", 3600)
        conn.execute("UPDATE auth_sessions SET expires_at=clock_timestamp()-interval '1 second' WHERE user_id=%s",
                     (user["id"],))
    assert client.get("/api/workspaces", headers={"Cookie": f"{SESSION_COOKIE}={token}"}).status_code == 401
    assert same_origin("https://relay.example.test", "https://relay.example.test/auth/callback")
    assert not same_origin("https://evil.example.test", "https://relay.example.test/auth/callback")
    assert not same_origin(None, "https://relay.example.test/auth/callback")


def test_workspace_membership_mutations_require_a_real_owner_session(client):
    response = client.put(f"/api/workspaces/{TEST_TENANT}/members", headers=ADMIN,
                          json={"user_id": str(uuid.uuid4()), "role": "VIEWER"})
    assert response.status_code == 403


def test_dashboard_uses_session_auth_and_requires_workspace_selection():
    from pathlib import Path

    html = Path(__file__).parents[1].joinpath("app", "static", "index.html").read_text(encoding="utf-8")
    assert "href=\"/auth/login\"" in html
    assert "credentials:'same-origin'" in html
    assert "X-Workspace-ID" in html
    assert "headers:{Authorization:'Bearer '+keyInput.value.trim()" not in html
    assert "$('slackConnect').textContent=connected?'Reconnect':'Connect'" in html
    assert "$('slackConnect').classList.toggle('hidden',!admin)" in html
    assert "if(cfg.worker_count===0)" in html
    assert "button.remove()" in html
    assert "managed outside API" in html


def test_oidc_configuration_requires_complete_https_credentials(monkeypatch):
    import app.settings as settings

    names = ("RELAYCORE_OIDC_ISSUER", "RELAYCORE_OIDC_CLIENT_ID",
             "RELAYCORE_OIDC_CLIENT_SECRET", "RELAYCORE_OIDC_REDIRECT_URI",
             "RELAYCORE_OIDC_STATE_SECRET", "RELAYCORE_OIDC_DISCOVERY_URL")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    assert settings.oidc_settings() is None

    config = {
        "RELAYCORE_OIDC_ISSUER": "https://identity.example.test",
        "RELAYCORE_OIDC_CLIENT_ID": "relaycore",
        "RELAYCORE_OIDC_CLIENT_SECRET": "client-secret",
        "RELAYCORE_OIDC_REDIRECT_URI": "https://relay.example.test/auth/callback",
        "RELAYCORE_OIDC_STATE_SECRET": "s" * 40,
    }
    for name, value in config.items():
        monkeypatch.setenv(name, value)
    assert settings.oidc_settings()["RELAYCORE_OIDC_DISCOVERY_URL"] == (
        "https://identity.example.test/.well-known/openid-configuration"
    )
    monkeypatch.setenv("RELAYCORE_OIDC_ISSUER", "http://identity.example.test")
    with pytest.raises(RuntimeError, match="HTTPS"):
        settings.oidc_settings()


def test_oidc_identity_rejects_unverified_or_wrong_issuer_claims():
    claims = {"iss": "https://identity.example.test", "sub": "subject-1",
              "email": "user@example.test", "email_verified": True, "name": "Example User"}
    assert verified_oidc_profile(claims, "https://identity.example.test") == (
        "https://identity.example.test", "subject-1", "user@example.test", "Example User"
    )
    for invalid in ({**claims, "email_verified": False},
                    {**claims, "email_verified": "true"},
                    {**claims, "iss": "https://attacker.example.test"},
                    {**claims, "sub": ""}, {**claims, "email": "invalid"}):
        with pytest.raises(ValueError):
            verified_oidc_profile(invalid, "https://identity.example.test")


def test_webhook_encryption_key_rejects_invalid_configuration(monkeypatch):
    from cryptography.fernet import Fernet
    from app.settings import secret_encryption_key

    generated = Fernet.generate_key().decode()
    monkeypatch.setenv("RELAYCORE_SECRET_ENCRYPTION_KEY", generated)
    assert secret_encryption_key() == generated.encode()
    monkeypatch.setenv("RELAYCORE_SECRET_ENCRYPTION_KEY", "not-a-fernet-key")
    with pytest.raises(RuntimeError, match="Fernet"):
        secret_encryption_key()
    monkeypatch.delenv("RELAYCORE_SECRET_ENCRYPTION_KEY")
    assert secret_encryption_key() is None
    with pytest.raises(RuntimeError, match="required"):
        secret_encryption_key(required=True)


def test_workspace_credentials_are_encrypted_scoped_rotatable_and_revocable(client, monkeypatch):
    from cryptography.fernet import Fernet
    from app.secretbox import decrypt_secret
    from app.settings import secret_encryption_key
    from app.store import workspace_credential_secret

    monkeypatch.setenv("RELAYCORE_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
    owner_email = "credential-owner@example.test"
    with client.app.state.pool.connection() as conn, conn.transaction():
        user = upsert_oidc_user(conn, "https://identity.example", str(uuid.uuid4()), owner_email, owner_email)
        token = create_auth_session(conn, user["id"], "credential-auth-test", 3600)
    headers = {"Cookie": f"{SESSION_COOKIE}={token}"}
    workspace = client.post("/api/workspaces", headers=headers, json={"name": "Credential Team"})
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]
    scoped_headers = {**headers, "X-Workspace-ID": workspace_id}
    path = f"/api/workspaces/{workspace_id}/credentials"
    first_secret = "ghp_" + "a" * 36
    rejected_short_secret = client.post(path, headers={**scoped_headers, "Idempotency-Key": "credential:short"},
                                        json={"provider": "github", "name": "short", "secret": "too-short"})
    assert rejected_short_secret.status_code == 422
    assert "too-short" not in rejected_short_secret.text
    rejected_extra_field = client.post(
        path, headers={**scoped_headers, "Idempotency-Key": "credential:extra"},
        json={"provider": "github", "name": "extra", "secret": first_secret, "unexpected": True},
    )
    assert rejected_extra_field.status_code == 422
    assert first_secret not in rejected_extra_field.text
    create_headers = {**scoped_headers, "Idempotency-Key": "credential:create:one"}
    created = client.post(path, headers=create_headers,
                          json={"provider": "github", "name": "build bot", "secret": first_secret})
    assert created.status_code == 201, created.text
    credential = created.json()
    assert credential["created"] is True and "secret" not in credential
    retried = client.post(path, headers=create_headers,
                          json={"provider": "github", "name": "build bot", "secret": first_secret})
    assert retried.status_code == 201 and retried.json()["created"] is False
    assert client.post(path, headers=create_headers,
                       json={"provider": "github", "name": "build bot", "secret": "ghp_" + "b" * 36}).status_code == 409

    key = secret_encryption_key(required=True)
    with client.app.state.pool.connection() as conn:
        stored = conn.execute(
            "SELECT encrypted_secret,secret_fingerprint FROM integration_credential_secrets WHERE credential_id=%s",
            (credential["id"],),
        ).fetchone()
        assert stored["encrypted_secret"] != first_secret
        assert decrypt_secret(stored["encrypted_secret"], key) == first_secret
        assert workspace_credential_secret(conn, workspace_id, credential["id"], key) == {
            "provider": "github", "allowed_host": None, "secret": first_secret,
        }
        assert workspace_credential_secret(conn, "another-workspace", credential["id"], key) is None

    second_secret = "ghp_" + "c" * 36
    rotate_headers = {**scoped_headers, "Idempotency-Key": "credential:rotate:one"}
    rotated = client.post(f"{path}/{credential['id']}/rotate", headers=rotate_headers,
                          json={"secret": second_secret})
    assert rotated.status_code == 200 and rotated.json()["version"] == 2
    assert client.post(f"{path}/{credential['id']}/rotate", headers=rotate_headers,
                       json={"secret": second_secret}).json()["created"] is False
    with client.app.state.pool.connection() as conn:
        assert workspace_credential_secret(conn, workspace_id, credential["id"], key) == {
            "provider": "github", "allowed_host": None, "secret": second_secret,
        }
    listed = client.get(path, headers=scoped_headers)
    assert listed.status_code == 200
    assert listed.json()[0]["version"] == 2
    assert "secret" not in listed.text and first_secret not in listed.text and second_secret not in listed.text
    assert client.delete(f"{path}/{credential['id']}", headers=scoped_headers).status_code == 204
    with client.app.state.pool.connection() as conn:
        assert workspace_credential_secret(conn, workspace_id, credential["id"], key) is None
        secrets = conn.execute(
            "SELECT count(*) AS n FROM integration_credential_secrets "
            "WHERE credential_id=%s AND revoked_at IS NOT NULL", (credential["id"],),
        ).fetchone()["n"]
        assert secrets == 2


def test_production_workflow_runs_only_allowlisted_http_steps(client, monkeypatch):
    import app.http_action as http_action
    import app.main as main
    import app.store as store

    monkeypatch.setenv("RELAYCORE_HTTP_ALLOWED_HOSTS", "hooks.example.com,other.example.com")
    owner_email = "production-http-owner@example.test"
    with client.app.state.pool.connection() as conn, conn.transaction():
        user = upsert_oidc_user(conn, "https://identity.example", str(uuid.uuid4()), owner_email, owner_email)
        token = create_auth_session(conn, user["id"], "production-http-auth-test", 3600)
    headers = {"Cookie": f"{SESSION_COOKIE}={token}"}
    workspace = client.post("/api/workspaces", headers=headers, json={"name": "Production HTTP Team"})
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]
    scoped_headers = {**headers, "X-Workspace-ID": workspace_id}
    credentials_path = f"/api/workspaces/{workspace_id}/credentials"
    stored_secret = "http-bearer-" + "x" * 24
    missing_host = client.post(credentials_path,
                               headers={**scoped_headers, "Idempotency-Key": "http:credential:no-host"},
                               json={"provider": "http", "name": "unbound", "secret": stored_secret})
    assert missing_host.status_code == 422 and stored_secret not in missing_host.text
    stored = client.post(credentials_path, headers={**scoped_headers, "Idempotency-Key": "http:credential"},
                         json={"provider": "http", "name": "build service", "secret": stored_secret,
                               "allowed_host": "hooks.example.com"})
    assert stored.status_code == 201, stored.text

    monkeypatch.setattr(main, "DEMO_MODE", False)
    monkeypatch.setattr(store, "DEMO_MODE", False)
    steps = [{"name": "notify build service", "action": "http", "payload": {
        "method": "POST", "url": "https://hooks.example.com/v1/events",
        "credential_id": stored.json()["id"], "body": {"event": "build.completed"},
    }}]
    definition = client.post("/api/workflow-definitions", headers={**scoped_headers,
                            "Idempotency-Key": "http:workflow:create"},
                             json={"title": "Notify build service", "steps": steps})
    assert definition.status_code == 201, definition.text
    unversioned = client.post("/api/workflows", headers={**scoped_headers,
                                "Idempotency-Key": "http:workflow:unversioned"},
                              json={"title": "Skip versioning", "steps": steps})
    assert unversioned.status_code == 422
    wrong_host = client.post("/api/workflow-definitions", headers={**scoped_headers,
                             "Idempotency-Key": "http:workflow:wrong-host"}, json={
        "title": "Credential host mismatch", "steps": [{"name": "exfiltrate", "action": "http", "payload": {
            "method": "POST", "url": "https://other.example.com/v1/events",
            "credential_id": stored.json()["id"], "body": {"event": "build.completed"},
        }}],
    })
    assert wrong_host.status_code == 422

    calls = []

    def send(payload, credential, idempotency_key, allowed_host, event_payload=None):
        calls.append((payload, credential, idempotency_key, allowed_host, event_payload))
        return {"status_code": 202, "response_bytes": 0, "content_type": "application/json"}

    monkeypatch.setattr(http_action, "execute_http_action", send)
    run_headers = {**scoped_headers, "Idempotency-Key": "http:workflow:run"}
    run = client.post(f"/api/workflow-definitions/{definition.json()['id']}/runs", headers=run_headers)
    assert run.status_code == 202, run.text
    drive_run(client, workspace_id, worker_id="production-http-test-worker")
    completed = get_run(client, run.json()["id"], workspace_id)
    assert completed["status"] == "completed"
    assert completed["side_effects"][0]["result"]["status_code"] == 202
    assert calls == [({**steps[0]["payload"], "timeout_seconds": 0.6}, stored_secret,
                       f"{run.json()['id']}:0", "hooks.example.com", None)]
    assert "timeout_seconds" not in steps[0]["payload"]
    repeated = client.post(f"/api/workflow-definitions/{definition.json()['id']}/runs", headers=run_headers)
    assert repeated.status_code == 202 and repeated.json()["created"] is False

    sandbox_actions = client.post("/api/workflows", headers={**scoped_headers,
                                   "Idempotency-Key": "http:sandbox-action"}, json={
        "title": "Sandbox action", "steps": [{"name": "fake", "action": "record", "payload": {}}],
    })
    assert sandbox_actions.status_code == 422
    webhook_path = f"/api/workspaces/{workspace_id}/webhooks"
    webhook = client.post(webhook_path, headers={**scoped_headers, "Idempotency-Key": "http:webhook"},
                          json={"name": "Build events"})
    assert webhook.status_code == 201, webhook.text
    trigger = {"endpoint_id": webhook.json()["id"], "event_type": "push"}
    mapped_steps = [{"name": "notify build service", "action": "http", "payload": {
        "method": "POST", "url": "https://hooks.example.com/v1/events",
        "credential_id": stored.json()["id"], "body": {
            "event": {"$event": "/type"},
            "repository": {"$event": "/repository/full_name"},
            "sender": {"$event": "/sender/login"},
        },
    }}]
    triggered_definition = client.post("/api/workflow-definitions", headers={**scoped_headers,
                                      "Idempotency-Key": "http:workflow:webhook"}, json={
        "title": "Notify for pushes", "steps": mapped_steps, "trigger": trigger,
    })
    assert triggered_definition.status_code == 201, triggered_definition.text
    assert triggered_definition.json()["version"] == 1
    assert client.post(f"/api/workflow-definitions/{triggered_definition.json()['id']}/runs",
                       headers={**scoped_headers, "Idempotency-Key": "http:workflow:webhook:manual"}).status_code == 422

    event_payload = {"type": "push", "repository": {"full_name": "example/service"},
                     "sender": {"login": "octocat"}}
    raw_event = json.dumps(event_payload, separators=(",", ":")).encode()
    event_key, timestamp = "build-event-1", str(int(time.time()))
    signed = timestamp.encode() + b"\n" + event_key.encode() + b"\n" + raw_event
    signature = hmac.new(webhook.json()["secret"].encode(), signed, hashlib.sha256).hexdigest()
    event_headers = {"Content-Type": "application/json", "X-RelayCore-Event-ID": event_key,
                     "X-RelayCore-Timestamp": timestamp,
                     "X-RelayCore-Signature": f"sha256={signature}"}
    endpoint_url = f"/hooks/{webhook.json()['id']}"
    accepted = client.post(endpoint_url, content=raw_event, headers=event_headers)
    assert accepted.status_code == 202, accepted.text
    triggered_run = accepted.json()["triggered_runs"][0]
    assert triggered_run["workflow_version_id"] == triggered_definition.json()["version_id"]
    duplicate = client.post(endpoint_url, content=raw_event, headers=event_headers)
    assert duplicate.status_code == 202 and duplicate.json()["duplicate"]
    assert duplicate.json()["triggered_runs"] == []
    event_id = accepted.json()["event_id"]
    from psycopg.types.json import Jsonb

    with client.app.state.pool.connection() as conn, conn.transaction():
        conn.execute("UPDATE incoming_events SET received_at=clock_timestamp()-interval '32 days' WHERE id=%s",
                     (event_id,))
        version_hash = conn.execute(
            "SELECT definition_hash FROM workflow_versions WHERE id=%s",
            (triggered_definition.json()["version_id"],),
        ).fetchone()["definition_hash"]
        failed_run = store.create_workflow(
            conn, workspace_id, "Expired event replay test", [{"name": "mapped call", "action": "http",
            "payload": {"body": {"$event": "/type"}}}], f"expired-event:{uuid.uuid4()}",
            "expired-event-test", workflow_version_id=triggered_definition.json()["version_id"],
            definition_hash=version_hash, trigger_event_id=event_id,
        )
        conn.execute("UPDATE tasks SET status='dead' WHERE id=%s", (failed_run["task_id"],))
        conn.execute("UPDATE workflow_runs SET status='failed',finished_at=clock_timestamp() WHERE id=%s",
                     (failed_run["id"],))
        dead_letter_id = conn.execute(
            """INSERT INTO dead_letters(tenant_id,run_id,task_id,attempts,error)
               VALUES (%s,%s,%s,1,%s) RETURNING id""",
            (workspace_id, failed_run["id"], failed_run["task_id"],
             Jsonb({"kind": "test", "message": "expired event replay"})),
        ).fetchone()["id"]
        other_id = str(uuid.uuid4())
        other_body = b'{"type":"older-unreferenced"}'
        conn.execute(
            """INSERT INTO incoming_events
               (id,workspace_id,endpoint_id,event_key,request_id,payload_sha256,raw_body,payload,event_type,received_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp()-interval '31 days')""",
            (other_id, workspace_id, webhook.json()["id"], f"old:{uuid.uuid4()}", "retention-test",
             hashlib.sha256(other_body).hexdigest(), other_body, Jsonb({"type": "older-unreferenced"}),
             "older-unreferenced"),
        )
        assert store.expire_webhook_payloads(conn, batch_size=1) == 1
        assert conn.execute("SELECT payload IS NOT NULL AS has_payload FROM incoming_events WHERE id=%s",
                            (event_id,)).fetchone()["has_payload"]
        assert conn.execute("SELECT payload IS NULL AS cleared FROM incoming_events WHERE id=%s",
                            (other_id,)).fetchone()["cleared"]

    next_steps = [{"name": "notify after v2", "action": "http", "payload": {
        **mapped_steps[0]["payload"], "body": {"event": "v2"},
    }}]
    published = client.post(
        f"/api/workflow-definitions/{triggered_definition.json()['id']}/versions",
        headers={**scoped_headers, "Idempotency-Key": "http:workflow:webhook:v2"},
        json={"title": "Notify for pushes v2", "steps": next_steps, "trigger": trigger},
    )
    assert published.status_code == 201 and published.json()["version"] == 2
    drive_run(client, workspace_id, worker_id="production-http-trigger-worker")
    triggered_result = get_run(client, triggered_run["run_id"], workspace_id)
    assert triggered_result["status"] == "completed"
    assert triggered_result["workflow_version_id"] == triggered_definition.json()["version_id"]
    assert triggered_result["trigger_event"]["type"] == "push"
    assert "payload" not in triggered_result["trigger_event"]
    assert calls[-1][4] == event_payload
    with client.app.state.pool.connection() as conn:
        assert store.expire_webhook_payloads(conn) == 1
        expired_event = conn.execute(
            "SELECT raw_body,payload,payload_sha256,event_type FROM incoming_events WHERE id=%s",
            (event_id,),
        ).fetchone()
    assert expired_event == {"raw_body": None, "payload": None,
                             "payload_sha256": hashlib.sha256(raw_event).hexdigest(), "event_type": "push"}
    duplicate_after_expiry = client.post(endpoint_url, content=raw_event,
                                         headers=event_headers)
    assert duplicate_after_expiry.status_code == 202 and duplicate_after_expiry.json()["duplicate"] is True
    replay_expired = client.post(f"/api/dead-letters/{dead_letter_id}/replay", headers=scoped_headers)
    assert replay_expired.status_code == 409 and "payload expired" in replay_expired.text
    with client.app.state.pool.connection() as conn:
        state = conn.execute(
            """SELECT d.replayed_at,t.status AS task_status,w.status AS run_status
               FROM dead_letters d JOIN tasks t ON t.id=d.task_id JOIN workflow_runs w ON w.id=d.run_id
               WHERE d.id=%s""", (dead_letter_id,),
        ).fetchone()
    assert state == {"replayed_at": None, "task_status": "dead", "run_status": "failed"}
    response_text = client.get(f"/api/workflows/{triggered_run['run_id']}", headers=scoped_headers).text
    assert "example/service" not in response_text and "octocat" not in response_text

    cancelled_run = client.post(f"/api/workflow-definitions/{definition.json()['id']}/runs",
                                headers={**scoped_headers, "Idempotency-Key": "http:workflow:cancel"})
    assert cancelled_run.status_code == 202, cancelled_run.text
    started, release = threading.Event(), threading.Event()

    def slow_send(_payload, _credential, _idempotency_key, _allowed_host, _event_payload=None):
        started.set()
        assert release.wait(5)
        return {"status_code": 202, "response_bytes": 0, "content_type": "application/json"}

    monkeypatch.setattr(http_action, "execute_http_action", slow_send)
    with client.app.state.pool.connection() as conn:
        task = claim_task(conn, "http-cancel-test-worker", 5, tenant_id=workspace_id)
    assert task and task["run_id"] == cancelled_run.json()["id"]

    from app.store import execute_step, fail_task

    def execute_in_worker():
        with connect(DATABASE_URL, row_factory=dict_row, autocommit=True) as conn:
            try:
                execute_step(conn, "http-cancel-test-worker", task, task["request_id"])
            except Exception as exc:
                fail_task(conn, task, "http-cancel-test-worker", exc, task["request_id"])

    worker = threading.Thread(target=execute_in_worker)
    worker.start()
    try:
        assert started.wait(3)
        cancelled = client.post(f"/api/workflows/{cancelled_run.json()['id']}/cancel", headers=scoped_headers)
        assert cancelled.status_code == 200
        with client.app.state.pool.connection() as conn:
            task_state = conn.execute("SELECT status FROM tasks WHERE id=%s", (task["id"],)).fetchone()
            assert task_state["status"] == "running"
        release.set()
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    after_cancel = get_run(client, cancelled_run.json()["id"], workspace_id)
    assert after_cancel["status"] == "cancelled"
    assert after_cancel["task_status"] == "cancelled"
    assert after_cancel["side_effects"][0]["result"]["status_code"] == 202
    assert any(event["kind"] == "side_effect.completed_after_cancel" for event in after_cancel["events"])


def test_permanent_provider_action_errors_dead_letter_without_retry(client):
    from app.http_action import PermanentActionError
    from app.store import fail_task

    response = make_workflow(client, steps=[{"name": "provider action", "action": "record", "payload": {}}])
    assert response.status_code == 202
    with client.app.state.pool.connection() as conn:
        task = claim_task(conn, "permanent-action-test-worker", 1.2, tenant_id=TEST_TENANT)
        assert task
        fail_task(conn, task, "permanent-action-test-worker", PermanentActionError("bad request"),
                  task["request_id"])
        row = conn.execute("SELECT status,attempts,max_attempts FROM tasks WHERE id=%s", (task["id"],)).fetchone()
        letters = conn.execute("SELECT attempts FROM dead_letters WHERE task_id=%s", (task["id"],)).fetchall()
    assert row == {"status": "dead", "attempts": 1, "max_attempts": 3}
    assert letters == [{"attempts": 1}]


def test_provider_retry_after_sets_the_durable_retry_delay(client):
    from app.http_action import RetryableActionError
    from app.store import fail_task

    tenant = random_tenant()
    with client.app.state.pool.connection() as conn, conn.transaction():
        run = create_workflow(conn, tenant, "Rate-limited action",
                              [{"name": "record", "action": "record", "payload": {}}],
                              f"retry-after:{uuid.uuid4()}", "retry-after-test")
    with client.app.state.pool.connection() as conn:
        task = claim_task(conn, "retry-after-test-worker", 1.2, tenant_id=tenant)
        assert task and task["run_id"] == run["id"]
        fail_task(conn, task, "retry-after-test-worker",
                  RetryableActionError("rate limited", retry_after=2), task["request_id"])
        state = conn.execute(
            """SELECT status,EXTRACT(EPOCH FROM (available_at-clock_timestamp())) AS retry_seconds
               FROM tasks WHERE id=%s""", (task["id"],),
        ).fetchone()
    assert state["status"] == "retry_wait"
    assert state["retry_seconds"] >= 1.8


def test_production_worker_dead_letters_sandbox_actions_without_effect(client, monkeypatch):
    import app.store as store
    from app.http_action import PermanentActionError

    tenant = random_tenant()
    monkeypatch.setattr(store, "DEMO_MODE", False)
    with client.app.state.pool.connection() as conn, conn.transaction():
        run = create_workflow(conn, tenant, "Old sandbox run",
                              [{"name": "fake payment", "action": "charge", "payload": {"amount": 25}}],
                              f"old-sandbox:{uuid.uuid4()}", "mode-guard-test")
    with client.app.state.pool.connection() as conn:
        task = claim_task(conn, "mode-guard-worker", 1.2, tenant_id=tenant)
        assert task and task["run_id"] == run["id"]
        with pytest.raises(PermanentActionError, match="execution mode"):
            store.execute_step(conn, "mode-guard-worker", task, task["request_id"])
        store.fail_task(conn, task, "mode-guard-worker", PermanentActionError("mode guard"), task["request_id"])
        assert conn.execute("SELECT count(*) AS n FROM side_effects WHERE run_id=%s",
                            (run["id"],)).fetchone()["n"] == 0
        state = conn.execute("SELECT status,attempts FROM tasks WHERE id=%s", (task["id"],)).fetchone()
        assert state == {"status": "dead", "attempts": 1}


def test_workspace_webhooks_verify_signatures_and_dedupe_replays(client, monkeypatch):
    from app.secretbox import decrypt_secret
    from app.settings import secret_encryption_key

    email = "webhook-owner@example.test"
    with client.app.state.pool.connection() as conn, conn.transaction():
        user = upsert_oidc_user(conn, "https://identity.example", str(uuid.uuid4()), email, email)
        session_token = create_auth_session(conn, user["id"], "webhook-auth-test", 3600)
    headers = {"Cookie": f"{SESSION_COOKIE}={session_token}"}
    created_workspace = client.post("/api/workspaces", headers=headers, json={"name": "Webhook Team"})
    assert created_workspace.status_code == 201, created_workspace.text
    workspace_id = created_workspace.json()["id"]
    workspace_headers = {**headers, "X-Workspace-ID": workspace_id}

    create_headers = {**workspace_headers, "Idempotency-Key": "webhook:create:one"}
    created = client.post(f"/api/workspaces/{workspace_id}/webhooks", headers=create_headers,
                          json={"name": "Build events"})
    assert created.status_code == 201, created.text
    endpoint_id, secret = created.json()["id"], created.json()["secret"]
    retried = client.post(f"/api/workspaces/{workspace_id}/webhooks", headers=create_headers,
                          json={"name": "Build events"})
    assert retried.json()["id"] == endpoint_id and retried.json()["secret"] == secret
    assert retried.json()["created"] is False
    assert client.post(f"/api/workspaces/{workspace_id}/webhooks", headers=create_headers,
                       json={"name": "Different name"}).status_code == 409
    listing = client.get(f"/api/workspaces/{workspace_id}/webhooks", headers=workspace_headers)
    assert listing.status_code == 200 and len(listing.json()) == 1
    assert "secret" not in listing.json()[0] and "encrypted_secret" not in listing.json()[0]
    trigger = {"endpoint_id": endpoint_id, "event_type": "push"}
    definition_headers = {**workspace_headers, "Idempotency-Key": "webhook-workflow:create"}
    definition_body = {"title": "Push workflow", "steps": [
        {"name": "record push", "action": "record", "payload": {"source": "webhook"}},
    ], "trigger": trigger}
    definition = client.post("/api/workflow-definitions", headers=definition_headers, json=definition_body)
    assert definition.status_code == 201, definition.text
    bad_trigger = {**definition_body, "trigger": {"endpoint_id": str(uuid.uuid4()), "event_type": "push"}}
    assert client.post("/api/workflow-definitions", headers={**workspace_headers,
                       "Idempotency-Key": "webhook-workflow:other-tenant"}, json=bad_trigger).status_code == 404

    def signed_headers(event_key: str, body: bytes, signing_secret: str, timestamp: str | None = None):
        timestamp = timestamp or str(int(time.time()))
        content = timestamp.encode() + b"\n" + event_key.encode() + b"\n" + body
        digest = hmac.new(signing_secret.encode(), content, hashlib.sha256).hexdigest()
        return {"X-RelayCore-Event-ID": event_key, "X-RelayCore-Timestamp": timestamp,
                "X-RelayCore-Signature": "sha256=" + digest, "Content-Type": "application/json"}

    body = b'{"type":"push","repository":"acme/relay","action":"push"}'
    endpoint_url = f"/hooks/{endpoint_id}"
    assert client.post(endpoint_url, content=body, headers=signed_headers("push-1", body, secret,
                      str(int(time.time()) - 600))).status_code == 401
    assert client.post(endpoint_url, content=body, headers={**signed_headers("bad-signature", body, secret),
                      "X-RelayCore-Signature": "sha256=" + "0" * 64}).status_code == 401
    too_large = b"{" + b" " * (256 * 1024)
    assert client.post(endpoint_url, content=too_large, headers=signed_headers("large", too_large, secret)).status_code == 413
    malformed = b"not-json"
    assert client.post(endpoint_url, content=malformed,
                       headers=signed_headers("malformed", malformed, secret)).status_code == 400
    non_object = b"[]"
    assert client.post(endpoint_url, content=non_object,
                       headers=signed_headers("non-object", non_object, secret)).status_code == 422

    import app.store as store
    queued_body = b'{"type":"push","repository":"acme/relay","action":"push"}'
    with monkeypatch.context() as patch:
        patch.setattr(store, "MAX_QUEUE_DEPTH", 0)
        blocked = client.post(endpoint_url, content=queued_body,
                              headers=signed_headers("queue-full", queued_body, secret))
        assert blocked.status_code == 429
    with client.app.state.pool.connection() as conn:
        assert conn.execute("SELECT 1 FROM incoming_events WHERE endpoint_id=%s AND event_key='queue-full'",
                            (endpoint_id,)).fetchone() is None

    first = client.post(endpoint_url, content=body, headers=signed_headers("push-1", body, secret))
    duplicate = client.post(endpoint_url, content=body, headers=signed_headers("push-1", body, secret))
    assert first.status_code == 202 and first.json()["duplicate"] is False
    assert len(first.json()["triggered_runs"]) == 1
    first_run = first.json()["triggered_runs"][0]
    assert first_run["version"] == 1 and first_run["created"] is True
    assert duplicate.status_code == 202 and duplicate.json()["duplicate"] is True
    assert duplicate.json()["triggered_runs"] == []
    changed_body = b'{"repository":"attacker/changed"}'
    assert client.post(endpoint_url, content=changed_body,
                       headers=signed_headers("push-1", changed_body, secret)).status_code == 409
    assert client.get(f"/api/workflows/{first_run['run_id']}", headers=workspace_headers).json()[
        "workflow_version_id"] == definition.json()["version_id"]

    version_body = {**definition_body, "title": "Push workflow v2", "trigger": trigger}
    version = client.post(f"/api/workflow-definitions/{definition.json()['id']}/versions",
                          headers={**workspace_headers, "Idempotency-Key": "webhook-workflow:version:two"},
                          json=version_body)
    assert version.status_code == 201, version.text

    with client.app.state.pool.connection() as conn:
        row = conn.execute("SELECT encrypted_secret FROM webhook_secrets WHERE endpoint_id=%s AND version=1",
                           (endpoint_id,)).fetchone()
        assert row["encrypted_secret"] != secret
        assert decrypt_secret(row["encrypted_secret"], secret_encryption_key()) == secret

    rotation_headers = {**workspace_headers, "Idempotency-Key": "webhook:rotate:one"}
    rotated = client.post(f"/api/workspaces/{workspace_id}/webhooks/{endpoint_id}/rotate",
                          headers=rotation_headers)
    assert rotated.status_code == 200, rotated.text
    next_secret = rotated.json()["secret"]
    assert next_secret != secret and rotated.json()["version"] == 2
    assert client.post(endpoint_url, content=body, headers=signed_headers("old-secret", body, secret)).status_code == 401
    retry_rotation = client.post(f"/api/workspaces/{workspace_id}/webhooks/{endpoint_id}/rotate",
                                 headers=rotation_headers)
    assert retry_rotation.json()["secret"] == next_secret and retry_rotation.json()["created"] is False
    unmatched_body = b'{"type":"issue","repository":"acme/relay"}'
    unmatched = client.post(endpoint_url, content=unmatched_body,
                             headers=signed_headers("issue-1", unmatched_body, next_secret))
    assert unmatched.status_code == 202 and unmatched.json()["triggered_runs"] == []
    accepted = client.post(endpoint_url, content=body, headers=signed_headers("push-2", body, next_secret))
    assert accepted.status_code == 202 and accepted.json()["duplicate"] is False
    assert accepted.json()["triggered_runs"][0]["version"] == 2
    import app.main as main
    production_body = b'{"type":"push","repository":"acme/relay","action":"push"}'
    with monkeypatch.context() as patch:
        patch.setattr(main, "DEMO_MODE", False)
        production = client.post(endpoint_url, content=production_body,
                                 headers=signed_headers("production-mode", production_body, next_secret))
        assert production.status_code == 202 and production.json()["triggered_runs"] == []

    with client.app.state.pool.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM incoming_events WHERE endpoint_id=%s",
                            (endpoint_id,)).fetchone()["n"] == 4
        events = conn.execute("SELECT data FROM events WHERE tenant_id=%s AND kind='webhook.received'",
                              (workspace_id,)).fetchall()
        assert len(events) == 4
        assert secret not in json.dumps(events)
    first_page = client.get("/api/events", headers=workspace_headers, params={"limit": 2})
    assert first_page.status_code == 200 and len(first_page.json()["items"]) == 2
    assert first_page.json()["next_cursor"]
    assert "payload" not in first_page.json()["items"][0]
    second_page = client.get("/api/events", headers=workspace_headers,
                             params={"limit": 2, "cursor": first_page.json()["next_cursor"]})
    assert len(second_page.json()["items"]) == 2 and second_page.json()["next_cursor"] is None
    assert client.get("/api/events", headers=workspace_headers,
                      params={"cursor": "invalid"}).status_code == 400
    assert client.delete(f"/api/workspaces/{workspace_id}/webhooks/{endpoint_id}",
                         headers=workspace_headers).status_code == 204
    assert client.post(endpoint_url, content=body,
                       headers=signed_headers("after-revoke", body, next_secret)).status_code == 404


def test_workflow_versions_are_immutable_and_runs_pin_the_selected_version(client):
    headers = {**ADMIN, "Idempotency-Key": f"definition:{uuid.uuid4()}",
               "X-Request-ID": "version-create-test"}
    first = client.post("/api/workflow-definitions", headers=headers, json={
        "title": "versioned flow",
        "steps": [{"name": "record value", "action": "record", "payload": {"value": 1}}],
    })
    assert first.status_code == 201, first.text
    workflow_id = first.json()["id"]
    assert first.headers["X-Request-ID"] == "version-create-test"
    assert client.post("/api/workflow-definitions", headers=headers, json={
        "title": "versioned flow",
        "steps": [{"name": "record value", "action": "record", "payload": {"value": 1}}],
    }).json()["created"] is False

    second = client.post(f"/api/workflow-definitions/{workflow_id}/versions",
                         headers={**ADMIN, "Idempotency-Key": f"version:{uuid.uuid4()}",
                                  "X-Request-ID": "version-publish-test"}, json={
        "title": "versioned flow",
        "steps": [{"name": "record value", "action": "record", "payload": {"value": 2}}],
    })
    assert second.status_code == 201, second.text
    with client.app.state.pool.connection() as conn:
        assert conn.execute(
            "SELECT request_id FROM events WHERE kind='workflow.version_published' AND data->>'workflow_id'=%s ORDER BY sequence",
            (workflow_id,),
        ).fetchall() == [
            {"request_id": "version-create-test"}, {"request_id": "version-publish-test"}
        ]
    version_id = second.json()["id"]

    run_key = f"version-run:{uuid.uuid4()}"
    run_response = client.post(f"/api/workflow-definitions/{workflow_id}/runs",
                               headers={**ADMIN, "Idempotency-Key": run_key})
    assert run_response.status_code == 202, run_response.text
    run_id = run_response.json()["id"]
    run_hash = get_run(client, run_id)["definition_hash"]

    third = client.post(f"/api/workflow-definitions/{workflow_id}/versions",
                        headers={**ADMIN, "Idempotency-Key": f"version:{uuid.uuid4()}"}, json={
        "title": "versioned flow",
        "steps": [{"name": "record value", "action": "record", "payload": {"value": 3}}],
    })
    assert third.status_code == 201, third.text
    retried = client.post(f"/api/workflow-definitions/{workflow_id}/runs",
                          headers={**ADMIN, "Idempotency-Key": run_key})
    assert retried.status_code == 202, retried.text
    assert retried.json()["id"] == run_id and retried.json()["created"] is False

    run = get_run(client, run_id)
    assert run["workflow_version_id"] == version_id
    assert run["definition"]["steps"][0]["payload"]["value"] == 2
    assert run["definition_hash"] == run_hash
    drive_run(client)
    assert get_run(client, run_id)["side_effects"][0]["result"]["value"] == 2
    with connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.transaction():
            with pytest.raises(RaiseException, match="workflow versions are immutable"):
                conn.execute("UPDATE workflow_versions SET version_number=99 WHERE id=%s", (version_id,))
        with conn.transaction():
            with pytest.raises(RaiseException, match="workflow versions are immutable"):
                conn.execute("DELETE FROM workflow_versions WHERE id=%s", (version_id,))
    versions = client.get(f"/api/workflow-definitions/{workflow_id}", headers=ADMIN).json()["versions"]
    assert [version["version_number"] for version in versions] == [1, 2, 3]
    assert "author_credential_fingerprint" not in versions[0]
    assert client.get(f"/api/workflow-definitions/{workflow_id}", headers=OTHER).status_code == 404


def test_schedule_api_is_scoped_idempotent_and_supports_pause_resume_cancel(client, monkeypatch):
    import app.store as store

    definition = client.post("/api/workflow-definitions", headers={
        **ADMIN, "Idempotency-Key": f"schedule-definition:{uuid.uuid4()}"
    }, json={
        "title": "scheduled flow",
        "steps": [{"name": "record value", "action": "record", "payload": {"value": 1}}],
    })
    assert definition.status_code == 201, definition.text
    workspace = TEST_TENANT
    path = f"/api/workspaces/{workspace}/schedules"
    body = {"workflow_id": definition.json()["id"], "name": "hourly check", "interval_seconds": 3600}
    key = f"schedule-create:{uuid.uuid4()}"

    assert client.post(path, headers={**VIEWER, "Idempotency-Key": key}, json=body).status_code == 403
    assert client.post(path, headers={**ADMIN, "Idempotency-Key": key},
                       json={**body, "interval_seconds": 59}).status_code == 422
    created = client.post(path, headers={**ADMIN, "Idempotency-Key": key}, json=body)
    repeated = client.post(path, headers={**ADMIN, "Idempotency-Key": key}, json=body)
    conflict = client.post(path, headers={**ADMIN, "Idempotency-Key": key},
                           json={**body, "interval_seconds": 7200})
    assert created.status_code == 201, created.text
    assert repeated.json()["id"] == created.json()["id"] and repeated.json()["created"] is False
    assert conflict.status_code == 409
    schedule_id = created.json()["id"]
    monkeypatch.setattr(store, "MAX_SCHEDULES_PER_TENANT", 1)
    full = client.post(path, headers={**ADMIN, "Idempotency-Key": f"schedule-full:{uuid.uuid4()}"},
                       json={**body, "name": "second schedule"})
    assert full.status_code == 429
    assert client.get(path, headers=OTHER).status_code == 404
    assert client.get(path, headers=ADMIN).json()[0]["id"] == schedule_id
    assert client.get(path, headers=VIEWER).status_code == 200

    paused = client.patch(f"{path}/{schedule_id}", headers=ADMIN, json={"status": "paused"})
    resumed = client.patch(f"{path}/{schedule_id}", headers=ADMIN, json={"status": "active"})
    assert paused.json()["status"] == "paused" and paused.json()["pause_reason"] == "user_paused"
    assert resumed.json()["status"] == "active" and resumed.json()["pause_reason"] is None
    assert client.delete(f"{path}/{schedule_id}", headers=ADMIN).json()["status"] == "cancelled"
    assert client.delete(f"{path}/{schedule_id}", headers=ADMIN).json()["status"] == "cancelled"
    assert client.patch(f"{path}/{schedule_id}", headers=ADMIN, json={"status": "active"}).status_code == 409


def test_scheduler_pins_each_current_version_and_pauses_event_dependent_workflows(client, monkeypatch):
    import app.coordinator as coordinator

    monkeypatch.setattr(coordinator, "dispatch_due_schedules", lambda _conn: 0)
    definition = client.post("/api/workflow-definitions", headers={
        **ADMIN, "Idempotency-Key": f"schedule-version-definition:{uuid.uuid4()}"
    }, json={
        "title": "scheduled version flow",
        "steps": [{"name": "record value", "action": "record", "payload": {"value": 1}}],
    })
    assert definition.status_code == 201, definition.text
    path = f"/api/workspaces/{TEST_TENANT}/schedules"
    schedule = client.post(path, headers={**ADMIN, "Idempotency-Key": f"schedule:{uuid.uuid4()}"}, json={
        "workflow_id": definition.json()["id"], "name": "version poll", "interval_seconds": 60,
    })
    assert schedule.status_code == 201, schedule.text
    schedule_id, workflow_id = schedule.json()["id"], definition.json()["id"]

    def make_due():
        with client.app.state.pool.connection() as conn:
            conn.execute("UPDATE workflow_schedules SET next_run_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                         (schedule_id,))

    make_due()
    start = threading.Barrier(2)
    results = []

    def dispatch_concurrently():
        with client.app.state.pool.connection() as conn:
            start.wait(5)
            results.append(dispatch_due_schedules(conn))

    workers = [threading.Thread(target=dispatch_concurrently) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=8)
    assert all(not worker.is_alive() for worker in workers)
    assert sum(results) == 1
    with client.app.state.pool.connection() as conn:
        assert dispatch_due_schedules(conn) == 0
        runs = conn.execute(
            """SELECT id,workflow_version_id,definition FROM workflow_runs
               WHERE tenant_id=%s AND idempotency_key LIKE %s ORDER BY created_at""",
            (TEST_TENANT, f"schedule:{schedule_id}:%"),
        ).fetchall()
    assert len(runs) == 1
    assert runs[0]["workflow_version_id"] == definition.json()["version_id"]
    assert runs[0]["definition"]["steps"][0]["payload"]["value"] == 1

    second = client.post(f"/api/workflow-definitions/{workflow_id}/versions", headers={
        **ADMIN, "Idempotency-Key": f"schedule-version:{uuid.uuid4()}"
    }, json={
        "title": "scheduled version flow",
        "steps": [{"name": "record value", "action": "record", "payload": {"value": 2}}],
    })
    assert second.status_code == 201, second.text
    make_due()
    with client.app.state.pool.connection() as conn:
        assert dispatch_due_schedules(conn) == 1

    third = client.post(f"/api/workflow-definitions/{workflow_id}/versions", headers={
        **ADMIN, "Idempotency-Key": f"schedule-version:{uuid.uuid4()}"
    }, json={
        "title": "event-dependent version",
        "steps": [{"name": "record event reference", "action": "record",
                   "payload": {"body": {"$event": "/order"}}}],
    })
    assert third.status_code == 201, third.text
    make_due()
    with client.app.state.pool.connection() as conn:
        assert dispatch_due_schedules(conn) == 0
        state = conn.execute(
            "SELECT status,pause_reason FROM workflow_schedules WHERE tenant_id=%s AND id=%s",
            (TEST_TENANT, schedule_id),
        ).fetchone()
        runs = conn.execute(
            """SELECT workflow_version_id,definition FROM workflow_runs
               WHERE tenant_id=%s AND idempotency_key LIKE %s ORDER BY created_at""",
            (TEST_TENANT, f"schedule:{schedule_id}:%"),
        ).fetchall()
    assert state == {"status": "paused", "pause_reason": "workflow_requires_webhook_event"}
    assert [run["workflow_version_id"] for run in runs] == [definition.json()["version_id"], second.json()["id"]]
    assert [run["definition"]["steps"][0]["payload"]["value"] for run in runs] == [1, 2]


def test_scheduler_leaves_due_occurrence_queued_when_tenant_queue_is_full(client, monkeypatch):
    import app.coordinator as coordinator
    import app.store as store

    monkeypatch.setattr(coordinator, "dispatch_due_schedules", lambda _conn: 0)
    definition = client.post("/api/workflow-definitions", headers={
        **ADMIN, "Idempotency-Key": f"schedule-full-definition:{uuid.uuid4()}"
    }, json={
        "title": "full queue scheduled flow",
        "steps": [{"name": "record value", "action": "record", "payload": {"value": 1}}],
    })
    assert definition.status_code == 201, definition.text
    path = f"/api/workspaces/{TEST_TENANT}/schedules"
    schedule = client.post(path, headers={**ADMIN, "Idempotency-Key": f"schedule:{uuid.uuid4()}"}, json={
        "workflow_id": definition.json()["id"], "name": "wait for capacity", "interval_seconds": 60,
    })
    assert schedule.status_code == 201, schedule.text
    schedule_id = schedule.json()["id"]

    other_definition = client.post("/api/workflow-definitions", headers={
        **OTHER, "Idempotency-Key": f"other-schedule-definition:{uuid.uuid4()}"
    }, json={
        "title": "healthy tenant scheduled flow",
        "steps": [{"name": "record value", "action": "record", "payload": {"value": 2}}],
    })
    assert other_definition.status_code == 201, other_definition.text
    other_path = f"/api/workspaces/{OTHER_TENANT}/schedules"
    other_schedule = client.post(other_path, headers={
        **OTHER, "Idempotency-Key": f"other-schedule:{uuid.uuid4()}"
    }, json={
        "workflow_id": other_definition.json()["id"], "name": "healthy tenant", "interval_seconds": 60,
    })
    assert other_schedule.status_code == 201, other_schedule.text
    other_schedule_id = other_schedule.json()["id"]

    with client.app.state.pool.connection() as conn:
        active = conn.execute(
            "SELECT count(*) AS count FROM tasks WHERE tenant_id=%s AND status IN ('queued','retry_wait','running')",
            (TEST_TENANT,),
        ).fetchone()["count"]
    monkeypatch.setattr(store, "MAX_QUEUE_DEPTH", active + 1)
    pending = make_workflow(client, key=f"queue-before-schedule:{uuid.uuid4()}")
    assert pending.status_code == 202, pending.text
    with client.app.state.pool.connection() as conn:
        conn.execute("UPDATE workflow_schedules SET next_run_at=clock_timestamp()-interval '2 seconds' WHERE id=%s",
                     (schedule_id,))
        conn.execute("UPDATE workflow_schedules SET next_run_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                     (other_schedule_id,))
        assert dispatch_due_schedules(conn) == 1
        state = conn.execute(
            "SELECT status,next_run_at<=clock_timestamp() AS is_due FROM workflow_schedules WHERE id=%s",
            (schedule_id,),
        ).fetchone()
        scheduled_runs = conn.execute(
            "SELECT count(*) AS count FROM workflow_runs WHERE tenant_id=%s AND idempotency_key LIKE %s",
            (TEST_TENANT, f"schedule:{schedule_id}:%"),
        ).fetchone()["count"]
        healthy_run_count = conn.execute(
            "SELECT count(*) AS count FROM workflow_runs WHERE tenant_id=%s AND idempotency_key LIKE %s",
            (OTHER_TENANT, f"schedule:{other_schedule_id}:%"),
        ).fetchone()["count"]
    assert state == {"status": "active", "is_due": True}
    assert scheduled_runs == 0
    assert healthy_run_count == 1


def test_api_coordinator_dispatches_due_schedule(client):
    definition = client.post("/api/workflow-definitions", headers={
        **ADMIN, "Idempotency-Key": f"coordinator-schedule-definition:{uuid.uuid4()}"
    }, json={
        "title": "coordinator scheduled flow",
        "steps": [{"name": "record value", "action": "record", "payload": {"value": 3}}],
    })
    assert definition.status_code == 201, definition.text
    path = f"/api/workspaces/{TEST_TENANT}/schedules"
    schedule = client.post(path, headers={**ADMIN, "Idempotency-Key": f"coordinator-schedule:{uuid.uuid4()}"}, json={
        "workflow_id": definition.json()["id"], "name": "coordinator poll", "interval_seconds": 60,
    })
    assert schedule.status_code == 201, schedule.text
    schedule_id = schedule.json()["id"]
    with client.app.state.pool.connection() as conn:
        conn.execute("UPDATE workflow_schedules SET next_run_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                     (schedule_id,))

    deadline = time.monotonic() + 4
    state = None
    while time.monotonic() < deadline:
        with client.app.state.pool.connection() as conn:
            state = conn.execute(
                "SELECT last_run_id,last_run_at FROM workflow_schedules WHERE id=%s", (schedule_id,),
            ).fetchone()
        if state["last_run_id"]:
            break
        time.sleep(0.05)
    assert state and state["last_run_id"]
    assert state["last_run_at"] is not None


def test_idempotent_workflow_creation_and_durable_history(client):
    key = f"persist:{uuid.uuid4()}"
    first = make_workflow(client, key=key)
    again = make_workflow(client, key=key)
    conflict = make_workflow(client, key=key, title="different body")
    assert first.status_code == 202 and first.json()["created"] is True
    assert again.json()["id"] == first.json()["id"] and again.json()["created"] is False
    assert conflict.status_code == 409
    drive_run(client)
    run = get_run(client, first.json()["id"])
    assert run["status"] == "completed"
    assert [event["kind"] for event in run["events"]].count("side_effect.applied") == 2
    with connect(DATABASE_URL, row_factory=dict_row) as conn:
        assert conn.execute("SELECT status FROM workflow_runs WHERE id=%s", (run["id"],)).fetchone()["status"] == "completed"


def test_committed_step_resumes_at_next_step_after_worker_loss(client):
    response = make_workflow(client, steps=[
        {"name": "commit first effect", "action": "record", "payload": {"step": 1}},
        {"name": "continue after worker loss", "action": "record", "payload": {"step": 2}},
    ])
    run_id = response.json()["id"]
    with client.app.state.pool.connection() as conn:
        conn.execute("UPDATE tasks SET available_at=clock_timestamp()-interval '1 year' WHERE run_id=%s", (run_id,))
        first = claim_task(conn, "worker-before-loss", 1.2)
        assert first and first["run_id"] == run_id
        from app.store import execute_step
        execute_step(conn, "worker-before-loss", first, first["request_id"])
        checkpoint = conn.execute(
            "SELECT status,step_index FROM tasks WHERE run_id=%s", (run_id,)
        ).fetchone()
        assert checkpoint == {"status": "queued", "step_index": 1}

        # The first effect and step advancement committed together. A replacement worker
        # continues at step two instead of repeating step one.
        conn.execute("UPDATE tasks SET available_at=clock_timestamp()-interval '1 year' WHERE run_id=%s", (run_id,))
        second = claim_task(conn, "worker-after-loss", 1.2)
        assert second and second["step_index"] == 1
        execute_step(conn, "worker-after-loss", second, second["request_id"])

    run = get_run(client, run_id)
    assert run["status"] == "completed"
    assert [effect["step_index"] for effect in run["side_effects"]] == [0, 1]
    assert sum(event["kind"] == "side_effect.applied" for event in run["events"]) == 2


def test_fail_once_retries_then_completes(client):
    response = make_workflow(client, steps=[{"name": "retry once", "action": "fail_once", "payload": {}}])
    drive_run(client)
    run = get_run(client, response.json()["id"])
    assert run["status"] == "completed"
    kinds = [event["kind"] for event in run["events"]]
    assert "task.retry_scheduled" in kinds
    assert kinds.count("task.claimed") == 2
    assert len(run["side_effects"]) == 1


def test_duplicate_business_event_has_one_workflow_and_one_effect_per_step(client):
    key = f"dup-{uuid.uuid4()}"
    response = client.post("/api/demo/duplicates", headers=ADMIN,
                           json={"event_key": key, "order": "o-123", "amount": 25, "currency": "USD"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["deliveries_received"] == 10 and data["distinct_events"] == 1
    drive_run(client)
    run = get_run(client, data["workflow_id"])
    assert len(run["side_effects"]) == 3
    with client.app.state.pool.connection() as conn:
        event = conn.execute("SELECT received_count FROM business_events WHERE tenant_id=%s AND event_key=%s",
                             (TEST_TENANT, key)).fetchone()
        assert event["received_count"] == 10
        assert conn.execute("SELECT count(*) AS n FROM workflow_runs WHERE tenant_id=%s AND idempotency_key=%s",
                            (TEST_TENANT, f"business-event:{key}")).fetchone()["n"] == 1
    conflict = client.post("/api/demo/duplicates", headers=ADMIN,
                           json={"event_key": key, "order": "changed", "amount": 25, "currency": "USD"})
    assert conflict.status_code == 409


def test_cancel_prevents_an_active_step_side_effect(client):
    response = make_workflow(client, steps=[{"name": "slow", "action": "sleep", "payload": {"seconds": 0.5}}])
    run_id = response.json()["id"]
    with client.app.state.pool.connection() as conn:
        task = claim_task(conn, "cancel-test", 1.2)
    assert task
    cancelled = client.post(f"/api/workflows/{run_id}/cancel", headers=ADMIN)
    assert cancelled.status_code == 200
    with client.app.state.pool.connection() as conn:
        from app.store import execute_step
        execute_step(conn, "cancel-test", task, task["request_id"])
    run = get_run(client, run_id)
    assert run["status"] == "cancelled"
    assert run["side_effects"] == []


def test_dlq_replay_is_admin_only_and_audited(client):
    response = client.post("/api/demo/dlq", headers=ADMIN, json={})
    assert response.status_code == 202
    run_id = response.json()["id"]
    drive_run(client, timeout=8)
    run = get_run(client, run_id)
    assert run["status"] == "failed"
    letters = client.get("/api/dead-letters", headers=ADMIN).json()
    letter = next(row for row in letters if row["run_id"] == run_id)
    assert client.post(f"/api/dead-letters/{letter['id']}/replay", headers=VIEWER).status_code == 403
    replayed = client.post(f"/api/dead-letters/{letter['id']}/replay", headers=ADMIN)
    assert replayed.status_code == 202
    assert get_run(client, run_id)["status"] == "queued"
    drive_run(client, timeout=8)
    run = get_run(client, run_id)
    assert run["status"] == "completed"
    assert sum(e["kind"] == "workflow.replayed" for e in run["events"]) == 1
    assert len(run["side_effects"]) == 1


def test_queue_capacity_is_enforced_per_tenant(client, monkeypatch):
    import app.store as store

    tenant = random_tenant()
    keys = json.loads(__import__("os").environ["RELAYCORE_API_KEYS"])
    secret = f"key-{tenant}-long-enough"
    keys[secret] = {"tenant_id": tenant, "role": "admin"}
    monkeypatch.setenv("RELAYCORE_API_KEYS", json.dumps(keys))
    monkeypatch.setattr(store, "MAX_QUEUE_DEPTH", 1)
    headers = {"Authorization": f"Bearer {secret}"}
    assert make_workflow(client, tenant_headers=headers).status_code == 202
    assert make_workflow(client, tenant_headers=headers).status_code == 429


def test_concurrent_duplicate_delivery_creates_one_logical_event(client):
    tenant, key = random_tenant(), f"race-{uuid.uuid4()}"
    payload = {"order": "racing-order", "amount": 12, "currency": "USD"}
    barrier = threading.Barrier(2)
    results, errors = [], []

    def deliver():
        try:
            with connect(DATABASE_URL, row_factory=dict_row, autocommit=True) as conn:
                barrier.wait(timeout=10)
                with conn.transaction():
                    results.append(ingest_business_event(conn, tenant, key, payload, "race-test"))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=deliver) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads), "duplicate deliveries did not finish within 10 seconds"
    assert not errors, errors
    assert len(results) == 2
    assert sum(result["created"] for result in results) == 1
    assert len({result["workflow_id"] for result in results}) == 1
    with client.app.state.pool.connection() as conn:
        event = conn.execute("SELECT received_count FROM business_events WHERE tenant_id=%s AND event_key=%s",
                             (tenant, key)).fetchone()
        assert event["received_count"] == 2


def test_concurrent_workflow_idempotency_returns_one_run(client):
    tenant, key = random_tenant(), f"same-request-{uuid.uuid4()}"
    barrier = threading.Barrier(2)
    results = []

    def submit():
        with connect(DATABASE_URL, row_factory=dict_row) as conn, conn.transaction():
            barrier.wait()
            results.append(create_workflow(
                conn, tenant, "same workflow", [{"name": "record", "action": "record", "payload": {}}], key, "race"
            ))

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 2
    assert sum(result["created"] for result in results) == 1
    assert len({result["id"] for result in results}) == 1


def test_concurrent_admissions_respect_tenant_queue_limit(client, monkeypatch):
    import app.store as store

    tenant = random_tenant()
    monkeypatch.setattr(store, "MAX_QUEUE_DEPTH", 1)
    barrier = threading.Barrier(2)
    outcomes = []

    def submit(index):
        try:
            with connect(DATABASE_URL, row_factory=dict_row) as conn, conn.transaction():
                barrier.wait()
                outcomes.append(create_workflow(
                    conn, tenant, f"workflow {index}",
                    [{"name": "record", "action": "record", "payload": {}}],
                    f"capacity-{index}-{uuid.uuid4()}", "race",
                ))
        except QueueFull:
            outcomes.append("full")

    threads = [threading.Thread(target=submit, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(outcomes) == 2
    assert sum(outcome == "full" for outcome in outcomes) == 1


def test_event_cursor_resumes_after_last_event_id():
    assert event_cursor(None, "42") == 42
    assert event_cursor(50, "42") == 50
    with pytest.raises(HTTPException) as error:
        event_cursor(None, "not-a-sequence")
    assert error.value.status_code == 400
    with pytest.raises(HTTPException):
        event_cursor(None, "-1")


def test_event_stream_reads_and_advances_cursor(client):
    import app.main as main
    from app.auth import Principal

    class DisconnectAfterOneChunk:
        disconnected = False

        async def is_disconnected(self):
            if self.disconnected:
                return True
            self.disconnected = True
            return False

    response = asyncio.run(main.event_stream(
        DisconnectAfterOneChunk(), after=0, last_event_id=None,
        user=Principal(TEST_TENANT, "admin", "sse-test"), pool=client.app.state.pool,
    ))

    async def read_stream():
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(read_stream())
    assert chunks and any("event: workflow" in chunk or "keepalive" in chunk for chunk in chunks)


def test_non_demo_mode_hides_worker_metadata_and_rejects_simulated_actions(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "DEMO_MODE", False)
    status = client.get("/api/status", headers=ADMIN)
    assert status.status_code == 200
    assert status.json()["workers"] == []
    response = make_workflow(client)
    assert response.status_code == 422


def test_tenant_event_ids_follow_transaction_commit_order(client):
    tenant = random_tenant()
    first_inserted, allow_first_commit = threading.Event(), threading.Event()
    second_started, second_inserted = threading.Event(), threading.Event()

    def write_first():
        with connect(DATABASE_URL, row_factory=dict_row) as conn, conn.transaction():
            emit_event(conn, tenant, "ordered.first")
            first_inserted.set()
            assert allow_first_commit.wait(5)

    def write_second():
        first_inserted.wait(5)
        second_started.set()
        with connect(DATABASE_URL, row_factory=dict_row) as conn, conn.transaction():
            emit_event(conn, tenant, "ordered.second")
            second_inserted.set()

    first = threading.Thread(target=write_first)
    second = threading.Thread(target=write_second)
    first.start()
    second.start()
    try:
        assert first_inserted.wait(5)
        assert second_started.wait(5)
        assert not second_inserted.wait(0.2)
    finally:
        allow_first_commit.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()

    with client.app.state.pool.connection() as conn:
        kinds = conn.execute(
            "SELECT kind FROM events WHERE tenant_id=%s ORDER BY sequence", (tenant,)
        ).fetchall()
    assert [row["kind"] for row in kinds] == ["ordered.first", "ordered.second"]


def test_versioned_migrations_bootstrap_and_skip_applied_files():
    schema = f"migration_test_{uuid.uuid4().hex}"
    with connect(DATABASE_URL, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        try:
            conn.execute(f'SET search_path TO "{schema}"')
            migrate(conn)
            migrate(conn)
            assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
                {"version": "001"}, {"version": "002"}, {"version": "003"},
                {"version": "004"}, {"version": "005"}, {"version": "006"}, {"version": "007"},
                {"version": "008"}, {"version": "009"}, {"version": "010"}, {"version": "011"},
                {"version": "012"}, {"version": "013"}, {"version": "014"}, {"version": "015"},
                {"version": "016"}
            ]
            assert conn.execute("SELECT count(*) AS n FROM tasks").fetchone()["n"] == 0
            assert conn.execute(
                """SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema()
                   AND table_name='workflow_runs' AND column_name='trigger_event_id'"""
            ).fetchone()
            tables = conn.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema=current_schema() AND table_name=ANY(%s) ORDER BY table_name""",
                (["users", "workspaces", "workspace_members", "auth_sessions", "identity_events"],),
            ).fetchall()
            assert len(tables) == 5
            webhook_tables = conn.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema=current_schema() AND table_name=ANY(%s)""",
                (["webhook_endpoints", "webhook_secrets", "incoming_events"],),
            ).fetchall()
            assert len(webhook_tables) == 3
            github_tables = conn.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema=current_schema() AND table_name=ANY(%s)""",
                (["github_oauth_states", "github_installations"],),
            ).fetchall()
            assert len(github_tables) == 2
            slack_tables = conn.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema=current_schema() AND table_name=ANY(%s)""",
                (["slack_oauth_states", "slack_installations"],),
            ).fetchall()
            assert len(slack_tables) == 2
            assert conn.execute(
                """SELECT 1 FROM information_schema.tables
                   WHERE table_schema=current_schema() AND table_name='workflow_schedules'"""
            ).fetchone()
            credential_tables = conn.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema=current_schema() AND table_name=ANY(%s)""",
                (["integration_credentials", "integration_credential_secrets"],),
            ).fetchall()
            assert len(credential_tables) == 2
            assert conn.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema=current_schema() AND table_name='integration_credentials'
                     AND column_name='allowed_host'"""
            ).fetchone() == {"column_name": "allowed_host"}
            assert conn.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema=current_schema() AND table_name='workflow_versions'
                     AND column_name='author_user_id'"""
            ).fetchone() == {"column_name": "author_user_id"}
            assert conn.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema=current_schema() AND table_name='workspace_api_tokens'"""
            ).fetchone() == {"table_name": "workspace_api_tokens"}
        finally:
            conn.execute("SET search_path TO public")
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_workflow_version_migration_upgrades_an_existing_001_database():
    from pathlib import Path

    schema = f"migration_upgrade_{uuid.uuid4().hex}"
    initial = Path(__file__).parents[1] / "app" / "migrations" / "001_initial.sql"
    with connect(DATABASE_URL, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        try:
            conn.execute(f'SET search_path TO "{schema}"')
            conn.execute(initial.read_text(encoding="utf-8"), prepare=False)
            conn.execute("""CREATE TABLE schema_migrations (
                           version TEXT PRIMARY KEY,
                           applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())""")
            conn.execute("INSERT INTO schema_migrations(version) VALUES ('001')")
            migrate(conn)
            assert conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall() == [
                {"version": "001"}, {"version": "002"}, {"version": "003"},
                {"version": "004"}, {"version": "005"}, {"version": "006"}, {"version": "007"},
                {"version": "008"}, {"version": "009"}, {"version": "010"}, {"version": "011"},
                {"version": "012"}, {"version": "013"}, {"version": "014"}, {"version": "015"},
                {"version": "016"}
            ]
            columns = conn.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema=%s AND table_name='workflow_runs' AND column_name='workflow_version_id'""",
                (schema,),
            ).fetchall()
            assert columns == [{"column_name": "workflow_version_id"}]
            assert conn.execute(
                """SELECT 1 FROM information_schema.columns WHERE table_schema=%s
                   AND table_name='workflow_runs' AND column_name='trigger_event_id'""",
                (schema,),
            ).fetchone()
            assert conn.execute(
                """SELECT 1 FROM information_schema.tables
                   WHERE table_schema=%s AND table_name='workspace_members'""", (schema,)
            ).fetchone()
            assert conn.execute(
                """SELECT 1 FROM information_schema.columns
                   WHERE table_schema=%s AND table_name='integration_credentials' AND column_name='allowed_host'""",
                (schema,),
            ).fetchone()
        finally:
            conn.execute("SET search_path TO public")
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
