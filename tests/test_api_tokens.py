from __future__ import annotations

import asyncio
import hashlib
import uuid

import app.auth as auth
import app.main as main
import app.store as store
import pytest
from app.auth import SESSION_COOKIE
from app.store import create_auth_session, upsert_oidc_user
from tests.conftest import TEST_TENANT


def create_workspace(client, email: str) -> tuple[str, str, dict[str, str]]:
    with client.app.state.pool.connection() as conn, conn.transaction():
        user = upsert_oidc_user(conn, "https://identity.example", str(uuid.uuid4()), email, email)
        session = create_auth_session(conn, user["id"], "api-token-test", 3600)
    headers = {"Cookie": f"{SESSION_COOKIE}={session}"}
    response = client.post("/api/workspaces", headers=headers, json={"name": "Token test team"})
    assert response.status_code == 201, response.text
    return user["id"], response.json()["id"], {**headers, "X-Workspace-ID": response.json()["id"]}


def use_production_auth(monkeypatch):
    monkeypatch.setattr(auth, "DEMO_MODE", False)
    monkeypatch.setattr(main, "DEMO_MODE", False)


def assert_event_stream_closes_after_revocation(client, principal, workspace_id, revoke):
    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    with client.app.state.pool.connection() as conn:
        cursor = conn.execute("SELECT coalesce(max(sequence),0) AS sequence FROM events WHERE tenant_id=%s",
                              (workspace_id,)).fetchone()["sequence"]
    response = asyncio.run(main.event_stream(
        ConnectedRequest(), after=cursor, last_event_id=None,
        user=principal, pool=client.app.state.pool,
    ))

    async def consume_and_revoke():
        iterator = response.body_iterator
        assert "keepalive" in await anext(iterator)
        revoke()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(iterator), timeout=2)

    asyncio.run(consume_and_revoke())


def test_event_stream_stops_after_session_revocation(client):
    user_id, workspace_id, session_headers = create_workspace(client, f"stream-session-{uuid.uuid4()}@example.test")
    raw_session = session_headers["Cookie"].split("=", 1)[1]
    session_hash = hashlib.sha256(raw_session.encode()).hexdigest()
    principal = auth.Principal(workspace_id, "admin", session_hash, user_id=user_id,
                              workspace_role="OWNER", session_token_hash=session_hash)

    def revoke():
        with client.app.state.pool.connection() as conn, conn.transaction():
            conn.execute("UPDATE auth_sessions SET revoked_at=clock_timestamp() WHERE token_hash=%s", (session_hash,))

    assert_event_stream_closes_after_revocation(client, principal, workspace_id, revoke)


def test_event_stream_stops_after_api_token_revocation(client, monkeypatch):
    user_id, workspace_id, session_headers = create_workspace(client, f"stream-token-{uuid.uuid4()}@example.test")
    use_production_auth(monkeypatch)
    response = client.post(f"/api/workspaces/{workspace_id}/api-tokens", headers=session_headers,
                           json={"name": "SSE client"})
    assert response.status_code == 201, response.text
    token_id, token_hash = response.json()["id"], hashlib.sha256(response.json()["token"].encode()).hexdigest()
    principal = auth.Principal(workspace_id, "admin", token_hash, user_id=user_id, workspace_role="OWNER")

    def revoke():
        with client.app.state.pool.connection() as conn, conn.transaction():
            conn.execute("UPDATE workspace_api_tokens SET revoked_at=clock_timestamp() WHERE id=%s", (token_id,))

    assert_event_stream_closes_after_revocation(client, principal, workspace_id, revoke)


def test_event_stream_stops_after_workspace_membership_removal(client):
    owner_id, workspace_id, _ = create_workspace(client, f"stream-owner-{uuid.uuid4()}@example.test")
    with client.app.state.pool.connection() as conn, conn.transaction():
        member = upsert_oidc_user(conn, "https://identity.example", str(uuid.uuid4()),
                                  f"stream-member-{uuid.uuid4()}@example.test", "Stream member")
        raw_session = create_auth_session(conn, member["id"], "stream-membership-test", 3600)
        session_hash = hashlib.sha256(raw_session.encode()).hexdigest()
        conn.execute("""INSERT INTO workspace_members(workspace_id,user_id,role,created_by)
                       VALUES (%s,%s,'DEVELOPER',%s)""", (workspace_id, member["id"], owner_id))
    principal = auth.Principal(workspace_id, "operator", session_hash, user_id=member["id"],
                              workspace_role="DEVELOPER", session_token_hash=session_hash)

    def revoke():
        with client.app.state.pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM workspace_members WHERE workspace_id=%s AND user_id=%s",
                         (workspace_id, member["id"]))

    assert_event_stream_closes_after_revocation(client, principal, workspace_id, revoke)


def test_workspace_api_token_is_scoped_hashed_revocable_and_one_time(client, monkeypatch):
    _, workspace_id, session_headers = create_workspace(client, f"token-{uuid.uuid4()}@example.test")
    second_workspace = client.post("/api/workspaces", headers=session_headers,
                                   json={"name": "Another workspace"}).json()["id"]
    assert second_workspace != workspace_id
    use_production_auth(monkeypatch)

    created = client.post(
        f"/api/workspaces/{workspace_id}/api-tokens",
        headers=session_headers,
        json={"name": "CI runner", "expires_in_days": 7},
    )
    assert created.status_code == 201, created.text
    result = created.json()
    token = result["token"]
    assert token.startswith("rca_")
    assert result["role_ceiling"] == "operator"
    assert "token" not in client.get(f"/api/workspaces/{workspace_id}/api-tokens", headers=session_headers).json()[0]

    with client.app.state.pool.connection() as conn:
        row = conn.execute("SELECT token_hash FROM workspace_api_tokens WHERE id=%s", (result["id"],)).fetchone()
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()

    bearer = {"Authorization": f"Bearer {token}", "X-Workspace-ID": workspace_id}
    assert client.post("/api/workspaces", headers=bearer, json={"name": "Token should not create this"}).status_code == 403
    listed = client.get("/api/workspaces", headers=bearer)
    assert listed.status_code == 200
    assert [workspace["id"] for workspace in listed.json()] == [workspace_id]
    assert [workspace["id"] for workspace in client.get("/api/me", headers=bearer).json()["workspaces"]] == [workspace_id]
    assert client.get(f"/api/workspaces/{workspace_id}/api-tokens", headers=bearer).status_code == 403
    assert client.get("/api/workspaces/not-the-bound-workspace/api-tokens", headers=bearer).status_code == 404

    revoked = client.delete(f"/api/workspaces/{workspace_id}/api-tokens/{result['id']}", headers=session_headers)
    assert revoked.status_code == 204
    assert client.get("/api/workspaces", headers=bearer).status_code == 401


def test_workspace_api_token_expiry_and_current_membership_role(client, monkeypatch):
    user_id, workspace_id, session_headers = create_workspace(client, f"token-expiry-{uuid.uuid4()}@example.test")
    client.post("/api/workspaces", headers=session_headers, json={"name": "Another workspace"})
    use_production_auth(monkeypatch)
    created = client.post(f"/api/workspaces/{workspace_id}/api-tokens", headers=session_headers,
                          json={"name": "Short-lived", "role_ceiling": "admin"})
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    token_id = created.json()["id"]
    bearer = {"Authorization": f"Bearer {token}"}
    assert client.get(f"/api/workspaces/{workspace_id}/api-tokens", headers=bearer).status_code == 200
    assert client.post(f"/api/workspaces/{workspace_id}/api-tokens", headers=bearer,
                       json={"name": "Delegated token"}).status_code == 403

    with client.app.state.pool.connection() as conn, conn.transaction():
        conn.execute("UPDATE workspace_members SET role='DEVELOPER' WHERE workspace_id=%s AND user_id=%s",
                     (workspace_id, user_id))
    assert client.get(f"/api/workspaces/{workspace_id}/api-tokens", headers=bearer).status_code == 403
    assert client.get("/api/workflows", headers=bearer).status_code == 200
    with client.app.state.pool.connection() as conn, conn.transaction():
        conn.execute("DELETE FROM workspace_members WHERE workspace_id=%s AND user_id=%s", (workspace_id, user_id))
    assert client.get("/api/workflows", headers=bearer).status_code == 404

    with client.app.state.pool.connection() as conn, conn.transaction():
        conn.execute("""UPDATE workspace_api_tokens SET created_at=created_at-interval '2 days',
                       expires_at=clock_timestamp()-interval '1 second' WHERE id=%s""", (token_id,))
    assert client.get("/api/workspaces", headers=bearer).status_code == 401


def test_api_token_role_ceiling_limits_workspace_actions(client, monkeypatch):
    _, workspace_id, session_headers = create_workspace(client, f"token-ceiling-{uuid.uuid4()}@example.test")
    use_production_auth(monkeypatch)
    path = f"/api/workspaces/{workspace_id}/api-tokens"
    viewer = client.post(path, headers=session_headers,
                         json={"name": "Read-only", "role_ceiling": "viewer"})
    operator = client.post(path, headers=session_headers, json={"name": "Workflow runner"})
    assert viewer.status_code == operator.status_code == 201
    assert viewer.json()["role_ceiling"] == "viewer"
    assert operator.json()["role_ceiling"] == "operator"

    definition = {"title": "Restricted workflow", "steps": [
        {"name": "Record", "action": "record", "payload": {"value": 1}},
    ]}
    viewer_headers = {"Authorization": f"Bearer {viewer.json()['token']}"}
    operator_headers = {"Authorization": f"Bearer {operator.json()['token']}"}
    assert client.get("/api/workflows", headers=viewer_headers).status_code == 200
    assert client.post("/api/workflow-definitions", headers=viewer_headers, json=definition).status_code == 403
    assert client.post("/api/workflow-definitions", headers=operator_headers, json=definition).status_code == 201
    assert client.get(f"/api/workspaces/{workspace_id}/credentials", headers=operator_headers).status_code == 403


def test_api_tokens_are_not_available_in_demo_mode(client, monkeypatch):
    monkeypatch.setattr(main, "DEMO_MODE", True)
    response = client.post(
        "/api/workspaces/relaycore-test/api-tokens",
        headers={"Authorization": "Bearer demo-key-change-me-32"},
        json={"name": "demo token"},
    )
    assert response.status_code in {403, 404}


def test_api_token_creation_obeys_workspace_write_rate_limit(client, monkeypatch):
    _, workspace_id, session_headers = create_workspace(client, f"token-limit-{uuid.uuid4()}@example.test")
    use_production_auth(monkeypatch)
    monkeypatch.setattr(store, "RATE_LIMIT_PER_MINUTE", 1)
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MINUTE", 1)
    path = f"/api/workspaces/{workspace_id}/api-tokens"
    assert client.post(path, headers=session_headers, json={"name": "First"}).status_code == 201
    assert client.post(path, headers=session_headers, json={"name": "Second"}).status_code == 429


def test_token_management_rejects_wrong_workspace_and_demo_identities(client, monkeypatch):
    _, workspace_id, session_headers = create_workspace(client, f"token-scope-{uuid.uuid4()}@example.test")
    use_production_auth(monkeypatch)
    assert client.post("/api/workspaces/other-workspace/api-tokens", headers=session_headers,
                       json={"name": "wrong workspace"}).status_code == 404
    assert client.delete(f"/api/workspaces/other-workspace/api-tokens/{uuid.uuid4()}",
                         headers=session_headers).status_code == 404
    assert client.delete(f"/api/workspaces/{workspace_id}/api-tokens/{uuid.uuid4()}",
                         headers=session_headers).status_code == 404

    monkeypatch.setattr(auth, "DEMO_MODE", True)
    demo_headers = {"Authorization": "Bearer demo-key-change-me-32"}
    assert client.post(f"/api/workspaces/{TEST_TENANT}/api-tokens", headers=demo_headers,
                       json={"name": "demo identity"}).status_code == 403
    assert client.delete(f"/api/workspaces/{TEST_TENANT}/api-tokens/{uuid.uuid4()}",
                         headers=demo_headers).status_code == 403
