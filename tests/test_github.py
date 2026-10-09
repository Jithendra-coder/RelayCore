from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.auth import SESSION_COOKIE
from app.github import app_jwt, github_settings, normalize_pull_request, verify_webhook
from app.http_action import PermanentActionError, RetryableActionError
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


def test_github_issue_comment_scopes_token_and_maps_pull_request_fields(monkeypatch):
    import app.github as github

    configure_github(monkeypatch)
    calls = []

    def fake_request(url, *, method, headers, body=None, timeout=8):
        calls.append((url, method, headers, body, timeout))
        return ({"token": "ghs_test-installation-token"} if url.endswith("/access_tokens") else
                {"id": 1234, "html_url": "https://github.com/acme/relay/issues/17#issuecomment-1234"})

    monkeypatch.setattr(github, "_github_request", fake_request)
    result = github.execute_issue_comment_action(github_settings(), {
        "installation_id": 456, "account_login": "acme", "status": "active",
    }, {
        "repository": {"$event": "/repository/full_name"},
        "issue_number": {"$event": "/pull_request/number"},
        "body": "RelayCore verified this pull request.",
    }, event_payload={"repository": {"full_name": "acme/relay"}, "pull_request": {"number": 17}},
       timeout_seconds=2)

    token_url, token_method, token_headers, token_body, token_timeout = calls[0]
    comment_url, comment_method, comment_headers, comment_body, comment_timeout = calls[1]
    assert token_url == "https://api.github.com/app/installations/456/access_tokens"
    assert token_method == "POST" and token_headers["Authorization"].startswith("Bearer ")
    assert json.loads(token_body) == {"repositories": ["relay"], "permissions": {"issues": "write"}}
    assert 0 < token_timeout <= 2
    assert comment_url == "https://api.github.com/repos/acme/relay/issues/17/comments"
    assert comment_method == "POST" and comment_headers["Authorization"] == "Bearer ghs_test-installation-token"
    assert json.loads(comment_body) == {"body": "RelayCore verified this pull request."}
    assert 0 < comment_timeout <= token_timeout
    assert result == {"comment_id": 1234, "html_url": "https://github.com/acme/relay/issues/17#issuecomment-1234"}


def test_github_issue_comment_checks_owner_and_classifies_rate_limits(monkeypatch):
    import app.github as github

    configure_github(monkeypatch)
    calls = []
    payload = {"repository": "acme/relay", "issue_number": 17, "body": "Verified."}
    integration = {"installation_id": 456, "account_login": "acme", "status": "active"}

    def unexpected_request(*_args, **_kwargs):
        calls.append(True)
        raise github.GitHubHTTPError(429, 2.5, False)

    monkeypatch.setattr(github, "_github_request", unexpected_request)
    with pytest.raises(PermanentActionError, match="does not belong"):
        github.execute_issue_comment_action(github_settings(), {**integration, "account_login": "other"}, payload)
    assert calls == []
    with pytest.raises(RetryableActionError) as retry:
        github.execute_issue_comment_action(github_settings(), integration, payload)
    assert retry.value.retry_after == 2.5


def test_github_secondary_rate_limits_are_retryable_and_permission_errors_are_not(monkeypatch):
    import app.github as github

    cases = [
        (403, {"Retry-After": "17"}, b'{"message":"You have exceeded a secondary rate limit."}', 17.0, True),
        (403, {}, b'{"message":"You have exceeded a secondary rate limit."}', 60.0, True),
        (429, {}, b'{"message":"Too many requests"}', 60.0, True),
        (403, {}, b'{"message":"Resource not accessible by integration"}', None, False),
    ]
    for status, headers, body, expected_delay, rate_limited in cases:
        error = HTTPError("https://api.github.com/test", status, "Forbidden", headers, BytesIO(body))

        class ErrorOpener:
            def open(self, *_args, **_kwargs):
                raise error

        monkeypatch.setattr(github, "build_opener", lambda *_args: ErrorOpener())
        with pytest.raises(github.GitHubHTTPError) as caught:
            github._github_request("https://api.github.com/test", method="GET", headers={})
        assert caught.value.rate_limited is rate_limited
        assert caught.value.retry_after == expected_delay
        action_error = RetryableActionError if rate_limited else PermanentActionError
        with pytest.raises(action_error):
            github._raise_github_action_error(caught.value)


def test_github_primary_rate_limit_uses_reset_header(monkeypatch):
    import app.github as github

    monkeypatch.setattr(github.time, "time", lambda: 1_000.0)
    error = HTTPError("https://api.github.com/test", 403, "Forbidden", {
        "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1042",
    }, BytesIO(b'{"message":"API rate limit exceeded"}'))

    class ErrorOpener:
        def open(self, *_args, **_kwargs):
            raise error

    monkeypatch.setattr(github, "build_opener", lambda *_args: ErrorOpener())
    with pytest.raises(github.GitHubHTTPError) as caught:
        github._github_request("https://api.github.com/test", method="GET", headers={})
    assert caught.value.rate_limited
    assert caught.value.retry_after == 42.0
    with pytest.raises(RetryableActionError) as retry:
        github._raise_github_action_error(caught.value)
    assert retry.value.retry_after == 42.0


def test_github_issue_comment_maps_missing_or_invalid_event_values(monkeypatch):
    import app.github as github

    configure_github(monkeypatch)
    monkeypatch.setattr(github, "_github_request", lambda *_args, **_kwargs: pytest.fail("No request expected."))
    integration = {"installation_id": 456, "account_login": "acme", "status": "active"}
    payload = {"repository": {"$event": "/repository/full_name"},
               "issue_number": {"$event": "/pull_request/number"}, "body": "Verified."}
    with pytest.raises(PermanentActionError, match="issue_number event field was not present"):
        github.execute_issue_comment_action(github_settings(), integration, payload,
                                            event_payload={"repository": {"full_name": "acme/relay"}})
    with pytest.raises(ValueError, match="event references"):
        github.validate_issue_comment_step({**payload, "body": {"$event": "pull_request/title"}})


def test_signed_pull_request_runs_repository_scoped_comment_action(client, monkeypatch):
    _, secret = configure_github(monkeypatch)
    import app.github as github
    import app.main as main
    import app.store as store

    monkeypatch.setattr(main, "DEMO_MODE", False)
    monkeypatch.setattr(store, "DEMO_MODE", False)
    _, workspace_id, headers = create_workspace(client, f"github-comment-{uuid.uuid4()}@example.test")
    installation_id = uuid.uuid4().int % 9_000_000_000_000 + 1_000_000_000
    link_installation(client, monkeypatch, headers, installation_id)
    connection = client.get(f"/api/workspaces/{workspace_id}/github", headers=headers).json()
    definition = client.post("/api/workflow-definitions", headers={**headers, "Idempotency-Key": "github:comment"},
                             json={"title": "Comment on pull request", "steps": [{
                                 "name": "comment", "action": "github_issue_comment", "payload": {
                                     "repository": {"$event": "/repository/full_name"},
                                     "issue_number": {"$event": "/pull_request/number"},
                                     "body": "RelayCore verified this pull request.",
                                 },
                             }], "trigger": {"endpoint_id": connection["endpoint_id"],
                                            "event_type": "github.pull_request.opened"}})
    assert definition.status_code == 201, definition.text

    calls = []

    def fake_request(url, *, method, headers, body=None, timeout=8):
        calls.append((url, method, headers, body, timeout))
        return ({"token": "ghs_test-installation-token"} if url.endswith("/access_tokens") else
                {"id": 9876, "html_url": "https://github.com/acme/relay/issues/17#issuecomment-9876"})

    monkeypatch.setattr(github, "_github_request", fake_request)
    event_body = pull_request_payload(installation_id)
    accepted = client.post("/integrations/github/webhook", content=event_body,
                           headers=sign_github(event_body, str(uuid.uuid4()), secret))
    assert accepted.status_code == 202 and len(accepted.json()["triggered_runs"]) == 1
    run_id = accepted.json()["triggered_runs"][0]["run_id"]
    from tests.conftest import drive_run, get_run

    drive_run(client, workspace_id, worker_id="github-comment-worker")
    result = get_run(client, run_id, workspace_id)
    assert result["status"] == "completed"
    assert result["side_effects"][0]["result"] == {
        "comment_id": 9876, "html_url": "https://github.com/acme/relay/issues/17#issuecomment-9876",
    }
    assert calls[0][0] == f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    assert json.loads(calls[0][3]) == {"repositories": ["relay"], "permissions": {"issues": "write"}}
    assert calls[1][0] == "https://api.github.com/repos/acme/relay/issues/17/comments"
    assert json.loads(calls[1][3]) == {"body": "RelayCore verified this pull request."}


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
