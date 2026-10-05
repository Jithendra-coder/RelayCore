from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.auth import SESSION_COOKIE
from app.github import app_jwt, github_settings, normalize_pull_request, verify_webhook
from app.store import create_auth_session, upsert_oidc_user


def configure_github(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    values = {
        "RELAYCORE_GITHUB_APP_SLUG": "relaycore-test-app",
        "RELAYCORE_GITHUB_APP_ID": "12345",
        "RELAYCORE_GITHUB_CLIENT_ID": "Iv1.test-client-id",
        "RELAYCORE_GITHUB_CLIENT_SECRET": "test-client-secret-value",
        "RELAYCORE_GITHUB_PRIVATE_KEY_B64": base64.b64encode(pem).decode(),
        "RELAYCORE_GITHUB_WEBHOOK_SECRET": "github-webhook-secret-for-tests-0123456789",
        "RELAYCORE_GITHUB_SETUP_URL": "https://relay.example.test/integrations/github/setup",
        "RELAYCORE_GITHUB_CALLBACK_URL": "https://relay.example.test/integrations/github/callback",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return key, values["RELAYCORE_GITHUB_WEBHOOK_SECRET"]


def create_workspace(client, email: str) -> tuple[str, str, dict[str, str]]:
    with client.app.state.pool.connection() as conn, conn.transaction():
        user = upsert_oidc_user(conn, "https://identity.example", str(uuid.uuid4()), email, email)
        session = create_auth_session(conn, user["id"], "github-test", 3600)
    headers = {"Cookie": f"{SESSION_COOKIE}={session}"}
    response = client.post("/api/workspaces", headers=headers, json={"name": "GitHub test team"})
    assert response.status_code == 201, response.text
    workspace_id = response.json()["id"]
    return user["id"], workspace_id, {**headers, "X-Workspace-ID": workspace_id}


def sign_github(body: bytes, event_id: str, secret: str, event="pull_request") -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "X-GitHub-Event": event,
            "X-GitHub-Delivery": event_id, "X-Hub-Signature-256": f"sha256={digest}"}


def pull_request_payload(installation_id: int, action: str = "opened") -> bytes:
    payload = {
        "action": action,
        "installation": {"id": installation_id},
        "repository": {"id": 4321, "full_name": "acme/relay", "html_url": "https://github.com/acme/relay"},
        "pull_request": {"id": 7654, "number": 17, "state": "open", "title": "Handle retries",
                         "body": "safe retry", "html_url": "https://github.com/acme/relay/pull/17",
                         "draft": False, "merged": False, "head": {"sha": "secret-extra-not-forwarded"}},
        "sender": {"id": 11, "login": "octocat", "email": "private@example.test"},
    }
    return json.dumps(payload, separators=(",", ":")).encode()


def link_installation(client, monkeypatch, headers, installation_id):
    import app.main as main

    start = client.post(f"/api/workspaces/{headers['X-Workspace-ID']}/github/install", headers=headers)
    assert start.status_code == 201, start.text
    state = parse_qs(urlsplit(start.json()["install_url"]).query)["state"][0]
    setup = client.get("/integrations/github/setup", params={"installation_id": installation_id, "state": state},
                       follow_redirects=False)
    assert setup.status_code == 302
    authorization = parse_qs(urlsplit(setup.headers["location"]).query)
    assert authorization["state"] == [state]
    assert authorization["code_challenge_method"] == ["S256"]
    with monkeypatch.context() as patch:
        patch.setattr(main, "exchange_code", lambda *_args: "never-stored-user-token")
        patch.setattr(main, "user_installation", lambda _config, token, value: {
            "id": value, "app_id": 12345,
        })
        patch.setattr(main, "app_installation", lambda _config, value: {
            "id": value, "app_id": 12345, "suspended_at": None,
            "account": {"id": 987, "login": "acme", "type": "Organization"},
        })
        callback = client.get("/integrations/github/callback", headers=headers,
                              params={"code": "one-time-code", "state": state}, follow_redirects=False)
    assert callback.status_code == 303
    assert callback.headers["location"] == "/?github=connected"
    return state, callback


def test_github_app_configuration_and_jwt_are_validated(monkeypatch):
    key, _ = configure_github(monkeypatch)
    config = github_settings()
    jwt = app_jwt(config, now=1_800_000_000)
    encoded_header, encoded_claims, encoded_signature = jwt.split(".")
    claims = json.loads(base64.urlsafe_b64decode(encoded_claims + "=="))
    signature = base64.urlsafe_b64decode(encoded_signature + "==")
    assert claims == {"iat": 1_799_999_940, "exp": 1_800_000_480, "iss": 12345}
    key.public_key().verify(signature, f"{encoded_header}.{encoded_claims}".encode(),
                            padding.PKCS1v15(), hashes.SHA256())
    monkeypatch.setenv("RELAYCORE_GITHUB_APP_ID", "bad")
    try:
        github_settings()
        raise AssertionError("invalid App ID was accepted")
    except RuntimeError as exc:
        assert "positive integer" in str(exc)


def test_github_webhook_verifies_raw_bytes_and_normalizes_only_supported_fields():
    secret = "github-webhook-secret-for-tests-0123456789"
    body = pull_request_payload(987654)
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_webhook(body, signature, secret)
    assert not verify_webhook(body + b" ", signature, secret)
    assert not verify_webhook(body, None, secret)
    normalized = normalize_pull_request(json.loads(body), "pull_request")
    assert normalized["type"] == "github.pull_request.opened"
    assert normalized["pull_request"]["number"] == 17
    assert "head" not in normalized["pull_request"]
    assert "email" not in normalized["sender"]
    assert normalize_pull_request({"action": "edited"}, "pull_request") is None
    assert normalize_pull_request({}, "issues") is None


def test_github_user_installation_lookup_uses_the_documented_paged_endpoint(monkeypatch):
    import app.github as github

    calls = []

    def fake_request(url, **_kwargs):
        calls.append(url)
        return ({"total_count": 101, "installations": [{"id": 10}]} if url.endswith("page=1") else
                {"total_count": 101, "installations": [{"id": 987654, "app_id": 12345}]})

    monkeypatch.setattr(github, "_github_request", fake_request)
    found = github.user_installation({}, "ephemeral-token", 987654)
    assert found["app_id"] == 12345
    assert len(calls) == 2 and all("/user/installations?" in url for url in calls)


def test_github_callback_requires_the_initiating_admin_and_matching_app(client, monkeypatch):
    configure_github(monkeypatch)
    _, _, owner_headers = create_workspace(client, f"github-owner-{uuid.uuid4()}@example.test")
    installation_id = uuid.uuid4().int % 9_000_000_000_000 + 1_000_000_000
    start = client.post(f"/api/workspaces/{owner_headers['X-Workspace-ID']}/github/install",
                        headers=owner_headers)
    state = parse_qs(urlsplit(start.json()["install_url"]).query)["state"][0]
    client.get("/integrations/github/setup", params={"installation_id": installation_id, "state": state},
               follow_redirects=False)
    _, _, other_headers = create_workspace(client, f"github-other-{uuid.uuid4()}@example.test")
    assert client.get("/integrations/github/callback", headers=other_headers,
                      params={"code": "wrong-user", "state": state}).status_code == 403

    import app.main as main
    with monkeypatch.context() as patch:
        patch.setattr(main, "exchange_code", lambda *_args: "temporary-token")
        patch.setattr(main, "user_installation", lambda _config, _token, value: {"id": value})
        patch.setattr(main, "app_installation", lambda _config, value: {
            "id": value, "app_id": 99999, "suspended_at": None,
            "account": {"id": 987, "login": "acme", "type": "Organization"},
        })
        response = client.get("/integrations/github/callback", headers=owner_headers,
                              params={"code": "wrong-app", "state": state})
    assert response.status_code == 403
    assert client.get(f"/api/workspaces/{owner_headers['X-Workspace-ID']}/github",
                      headers=owner_headers).json() == {"connected": False}


def test_github_installation_oauth_boundary_and_webhook_trigger_deduplication(client, monkeypatch):
    _, secret = configure_github(monkeypatch)
    _, workspace_id, headers = create_workspace(client, f"github-{uuid.uuid4()}@example.test")
    installation_id = uuid.uuid4().int % 9_000_000_000_000 + 1_000_000_000
    state, _ = link_installation(client, monkeypatch, headers, installation_id)

    import app.main as main
    with monkeypatch.context() as patch:
        patch.setattr(main, "exchange_code", lambda *_args: "unused")
        patch.setattr(main, "user_installation", lambda *_args: {"id": installation_id})
        patch.setattr(main, "app_installation", lambda *_args: {
            "id": installation_id, "app_id": 99999, "suspended_at": None,
            "account": {"id": 987, "login": "acme", "type": "Organization"},
        })
        assert client.get("/integrations/github/callback", headers=headers,
                          params={"code": "replay", "state": state}).status_code == 403

    connection = client.get(f"/api/workspaces/{workspace_id}/github", headers=headers)
    assert connection.status_code == 200 and connection.json()["connected"]
    endpoint_id = connection.json()["endpoint_id"]
    listed = client.get(f"/api/workspaces/{workspace_id}/webhooks", headers=headers)
    assert all(item["id"] != endpoint_id for item in listed.json())

    definition = client.post("/api/workflow-definitions", headers={**headers, "Idempotency-Key": "github:trigger"},
                             json={"title": "Record opened PR", "steps": [
                                 {"name": "record", "action": "record", "payload": {"source": "github"}},
                             ], "trigger": {"endpoint_id": endpoint_id,
                                            "event_type": "github.pull_request.opened"}})
    assert definition.status_code == 201, definition.text
    body = pull_request_payload(installation_id)
    delivery = str(uuid.uuid4())
    path = "/integrations/github/webhook"
    unsigned = {**sign_github(body, delivery, secret), "X-Hub-Signature-256": "sha256=" + "0" * 64}
    assert client.post(path, content=body, headers=unsigned).status_code == 401
    first = client.post(path, content=body, headers=sign_github(body, delivery, secret))
    duplicate = client.post(path, content=body, headers=sign_github(body, delivery, secret))
    assert first.status_code == 202 and len(first.json()["triggered_runs"]) == 1
    assert duplicate.status_code == 202 and duplicate.json()["duplicate"] is True
    assert duplicate.json()["triggered_runs"] == []
    assert "secret-extra-not-forwarded" not in client.get(
        f"/api/workflows/{first.json()['triggered_runs'][0]['run_id']}", headers=headers
    ).text

    suspend_body = json.dumps({"action": "suspend", "installation": {"id": installation_id}},
                              separators=(",", ":")).encode()
    suspended = client.post(path, content=suspend_body,
                            headers=sign_github(suspend_body, str(uuid.uuid4()), secret, "installation"))
    assert suspended.status_code == 202
    ignored = client.post(path, content=body,
                          headers=sign_github(body, str(uuid.uuid4()), secret))
    assert ignored.status_code == 202 and ignored.json()["ignored"] is True
    assert client.delete(f"/api/workspaces/{workspace_id}/github", headers=headers).status_code == 204
    assert client.get(f"/api/workspaces/{workspace_id}/github", headers=headers).json()["connected"] is False
    late_unsuspend = json.dumps({"action": "unsuspend", "installation": {"id": installation_id}},
                                separators=(",", ":")).encode()
    client.post(path, content=late_unsuspend,
                headers=sign_github(late_unsuspend, str(uuid.uuid4()), secret, "installation"))
    assert client.get(f"/api/workspaces/{workspace_id}/github", headers=headers).json()["connected"] is False
    replacement_id = uuid.uuid4().int % 9_000_000_000_000 + 1_000_000_000
    link_installation(client, monkeypatch, headers, replacement_id)
    replacement = client.get(f"/api/workspaces/{workspace_id}/github", headers=headers).json()
    assert replacement["connected"] and replacement["installation_id"] == replacement_id
