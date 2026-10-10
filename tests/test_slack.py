from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest

from app.auth import SESSION_COOKIE
from app.settings import secret_encryption_key
from app.slack import (authorization_url, exchange_code, normalize_event, post_message, slack_settings,
                       validate_message_step, verify_request)
from app.store import create_auth_session, upsert_oidc_user, workspace_credential_secret


APP_ID = "A123TEST"
SIGNING_SECRET = "slack-signing-secret-for-tests-0123456789"
BOT_TOKEN = "xoxb-test-token-0123456789abcdefghijkl"


def configure_slack(monkeypatch):
    values = {
        "RELAYCORE_SLACK_CLIENT_ID": "123456.789012",
        "RELAYCORE_SLACK_CLIENT_SECRET": "slack-client-secret-for-tests",
        "RELAYCORE_SLACK_APP_ID": APP_ID,
        "RELAYCORE_SLACK_SIGNING_SECRET": SIGNING_SECRET,
        "RELAYCORE_SLACK_CALLBACK_URL": "https://relay.example.test/integrations/slack/callback",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def create_workspace(client, email: str) -> tuple[str, str, dict[str, str]]:
    with client.app.state.pool.connection() as conn, conn.transaction():
        user = upsert_oidc_user(conn, "https://identity.example", str(uuid.uuid4()), email, email)
        session = create_auth_session(conn, user["id"], "slack-test", 3600)
    headers = {"Cookie": f"{SESSION_COOKIE}={session}"}
    response = client.post("/api/workspaces", headers=headers, json={"name": "Slack test team"})
    assert response.status_code == 201, response.text
    workspace_id = response.json()["id"]
    return user["id"], workspace_id, {**headers, "X-Workspace-ID": workspace_id}


def grant(team_id="T123TEST", token=BOT_TOKEN) -> dict:
    return {"ok": True, "app_id": APP_ID, "token_type": "bot", "access_token": token,
            "scope": "app_mentions:read,chat:write", "bot_user_id": "U123TEST",
            "team": {"id": team_id, "name": "Relay Team"}}


def sign_slack(body: bytes, secret: str = SIGNING_SECRET, timestamp: str | None = None) -> dict[str, str]:
    timestamp = timestamp or str(int(time.time()))
    base = b"v0:" + timestamp.encode() + b":" + body
    signature = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": signature}


def mention_payload(event_id="Ev123TEST", *, app_id=APP_ID, team_id="T123TEST") -> bytes:
    return json.dumps({"type": "event_callback", "api_app_id": app_id, "team_id": team_id,
                       "token": "legacy-verification-token",
                       "event_id": event_id, "event": {"type": "app_mention", "channel": "C123TEST",
                       "user": "U456TEST", "ts": "1710000000.000100", "text": "please run"}},
                      separators=(",", ":")).encode()


def connect_slack(client, monkeypatch, headers, grant_data=None):
    import app.main as main

    start = client.post(f"/api/workspaces/{headers['X-Workspace-ID']}/slack/install", headers=headers)
    assert start.status_code == 201, start.text
    query = parse_qs(urlsplit(start.json()["install_url"]).query)
    state = query["state"][0]
    assert query["scope"] == ["app_mentions:read,chat:write"]
    assert query["redirect_uri"] == ["https://relay.example.test/integrations/slack/callback"]
    with monkeypatch.context() as patch:
        patch.setattr(main, "slack_exchange_code", lambda *_args: grant_data or grant())
        response = client.get("/integrations/slack/callback", headers=headers,
                              params={"code": "one-time-code", "state": state}, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/?slack=connected"
    return response


def test_slack_configuration_oauth_and_signature(monkeypatch):
    configure_slack(monkeypatch)
    config = slack_settings()
    query = parse_qs(urlsplit(authorization_url(config, "state-token")).query)
    assert query["client_id"] == [config["client_id"]]
    assert query["state"] == ["state-token"]
    assert query["scope"] == ["app_mentions:read,chat:write"]
    monkeypatch.setenv("RELAYCORE_SLACK_APP_ID", "invalid")
    with pytest.raises(RuntimeError, match="APP_ID"):
        slack_settings()

    body = b'{"type":"url_verification","challenge":"safe"}'
    timestamp = "1800000000"
    signature = "v0=" + hmac.new(SIGNING_SECRET.encode(), b"v0:" + timestamp.encode() + b":" + body,
                                  hashlib.sha256).hexdigest()
    assert verify_request(body, timestamp, signature, SIGNING_SECRET, now=1_800_000_000)
    assert not verify_request(body + b" ", timestamp, signature, SIGNING_SECRET, now=1_800_000_000)
    assert not verify_request(body, timestamp, signature, SIGNING_SECRET, now=1_800_000_301)
    assert not verify_request(body, "bad", signature, SIGNING_SECRET, now=1_800_000_000)


def test_slack_event_normalization_is_narrow_and_bounded():
    payload = json.loads(mention_payload())
    normalized = normalize_event(payload)
    assert normalized["type"] == "slack.app_mention"
    assert normalized["event"]["text"] == "please run"
    assert "api_app_id" not in normalized
    assert normalize_event({**payload, "event": {"type": "reaction_added"}}) is None
    with pytest.raises(ValueError):
        normalize_event({**payload, "event": {"type": "app_mention", "text": "x" * 4001}})


def test_slack_oauth_exchange_uses_fixed_endpoint_and_basic_auth(monkeypatch):
    import app.slack as slack

    calls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, limit):
            assert limit == 1_000_001
            return json.dumps(grant()).encode()

    class FakeOpener:
        def open(self, request, timeout):
            calls.append((request.full_url, request.data, request.headers, timeout))
            return FakeResponse()

    monkeypatch.setattr(slack, "build_opener", lambda *_args: FakeOpener())
    config = {"client_id": "123456.789012", "client_secret": "client-secret",
              "callback_url": "https://relay.example.test/integrations/slack/callback"}
    assert exchange_code(config, "one-time-code")["access_token"] == BOT_TOKEN
    url, body, headers, timeout = calls[0]
    assert url == "https://slack.com/api/oauth.v2.access"
    assert b"code=one-time-code" in body
    assert headers["Authorization"].startswith("Basic ") and timeout == 8


def test_slack_oauth_and_events_are_workspace_scoped_durable_and_revocable(client, monkeypatch):
    configure_slack(monkeypatch)
    _, workspace_id, headers = create_workspace(client, f"slack-{uuid.uuid4()}@example.test")
    team_id = "T" + uuid.uuid4().hex[:12].upper()
    connect_slack(client, monkeypatch, headers, grant(team_id))

    connection = client.get(f"/api/workspaces/{workspace_id}/slack", headers=headers)
    assert connection.status_code == 200 and connection.json()["connected"]
    assert connection.json()["team_name"] == "Relay Team"
    assert BOT_TOKEN not in json.dumps(connection.json())
    endpoint_id = connection.json()["endpoint_id"]
    listed = client.get(f"/api/workspaces/{workspace_id}/webhooks", headers=headers)
    assert all(item["id"] != endpoint_id for item in listed.json())
    with client.app.state.pool.connection() as conn:
        installation = conn.execute(
            "SELECT credential_id FROM slack_installations WHERE workspace_id=%s AND status='active'",
            (workspace_id,),
        ).fetchone()
        secret = workspace_credential_secret(
            conn, workspace_id, installation["credential_id"],
            secret_encryption_key(),
        )
        assert secret["provider"] == "slack" and secret["secret"] == BOT_TOKEN

    import app.main as main
    reconnect = client.post(f"/api/workspaces/{workspace_id}/slack/install", headers=headers)
    reconnect_state = parse_qs(urlsplit(reconnect.json()["install_url"]).query)["state"][0]
    next_token = "xoxb-reconnected-token-0123456789"
    with monkeypatch.context() as patch:
        patch.setattr(main, "slack_exchange_code", lambda *_args: grant(team_id, next_token))
        response = client.get("/integrations/slack/callback", headers=headers,
                              params={"code": "reconnect", "state": reconnect_state}, follow_redirects=False)
    assert response.status_code == 303
    with client.app.state.pool.connection() as conn:
        assert workspace_credential_secret(
            conn, workspace_id, installation["credential_id"], secret_encryption_key()
        )["secret"] == next_token

    definition = client.post(
        "/api/workflow-definitions", headers={**headers, "Idempotency-Key": "slack:trigger"},
        json={"title": "Record Slack mentions", "steps":[
            {"name": "record", "action": "record", "payload": {"source": "slack"}},
        ], "trigger": {"endpoint_id": endpoint_id, "event_type": "slack.app_mention"}},
    )
    assert definition.status_code == 201, definition.text

    now = str(int(time.time()))
    challenge_body = b'{"type":"url_verification","challenge":"challenge-value"}'
    challenge = client.post("/integrations/slack/events", content=challenge_body,
                            headers=sign_slack(challenge_body, timestamp=now))
    assert challenge.status_code == 200 and challenge.json() == {"challenge": "challenge-value"}

    body = mention_payload(team_id=team_id)
    path = "/integrations/slack/events"
    invalid = {**sign_slack(body, timestamp=now), "X-Slack-Signature": "v0=" + "0" * 64}
    assert client.post(path, content=body, headers=invalid).status_code == 401
    wrong_app = mention_payload(app_id="AOTHER")
    assert client.post(path, content=wrong_app, headers=sign_slack(wrong_app, timestamp=now)).status_code == 401
    first = client.post(path, content=body, headers=sign_slack(body, timestamp=now))
    duplicate = client.post(path, content=body, headers=sign_slack(body, timestamp=now))
    assert first.status_code == 200 and len(first.json()["triggered_runs"]) == 1, first.text
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"] is True
    with client.app.state.pool.connection() as conn:
        stored = conn.execute("SELECT raw_body,payload FROM incoming_events WHERE endpoint_id=%s AND event_key=%s",
                              (endpoint_id, "Ev123TEST")).fetchone()
    assert b"legacy-verification-token" not in stored["raw_body"]
    assert stored["payload"]["type"] == "slack.app_mention"

    uninstall = json.dumps({"type": "event_callback", "api_app_id": APP_ID, "team_id": team_id,
                            "event_id": "EvUninstall", "event": {"type": "app_uninstalled"}},
                           separators=(",", ":")).encode()
    assert client.post(path, content=uninstall, headers=sign_slack(uninstall)).status_code == 200
    after_uninstall = mention_payload("EvAfterUninstall", team_id=team_id)
    ignored = client.post(path, content=after_uninstall, headers=sign_slack(after_uninstall))
    assert ignored.status_code == 200 and ignored.json()["ignored"] is True
    assert client.get(f"/api/workspaces/{workspace_id}/slack", headers=headers).json()["connected"] is False
    with client.app.state.pool.connection() as conn:
        assert workspace_credential_secret(
            conn, workspace_id, installation["credential_id"], secret_encryption_key()
        ) is None
    assert client.delete(f"/api/workspaces/{workspace_id}/slack", headers=headers).status_code == 204


def test_slack_link_requires_configuration_and_workspace_admin(client, monkeypatch):
    for name in ("RELAYCORE_SLACK_CLIENT_ID", "RELAYCORE_SLACK_CLIENT_SECRET", "RELAYCORE_SLACK_APP_ID",
                 "RELAYCORE_SLACK_SIGNING_SECRET", "RELAYCORE_SLACK_CALLBACK_URL"):
        monkeypatch.delenv(name, raising=False)
    _, workspace_id, headers = create_workspace(client, f"slack-admin-{uuid.uuid4()}@example.test")
    response = client.post(f"/api/workspaces/{workspace_id}/slack/install", headers=headers)
    assert response.status_code == 503
    configure_slack(monkeypatch)
    from tests.conftest import TEST_TENANT
    assert client.post(f"/api/workspaces/{TEST_TENANT}/slack/install",
                       headers={"Authorization": "Bearer viewer-key-change-me-32"}).status_code == 403


def test_slack_team_cannot_be_linked_to_two_active_workspaces(client, monkeypatch):
    configure_slack(monkeypatch)
    team_id = "T" + uuid.uuid4().hex[:12].upper()
    _, _, first_headers = create_workspace(client, f"slack-first-{uuid.uuid4()}@example.test")
    _, second_workspace, second_headers = create_workspace(client, f"slack-second-{uuid.uuid4()}@example.test")
    connect_slack(client, monkeypatch, first_headers, grant(team_id))

    start = client.post(f"/api/workspaces/{second_workspace}/slack/install", headers=second_headers)
    state = parse_qs(urlsplit(start.json()["install_url"]).query)["state"][0]
    import app.main as main
    with monkeypatch.context() as patch:
        patch.setattr(main, "slack_exchange_code", lambda *_args: grant(team_id))
        response = client.get("/integrations/slack/callback", headers=second_headers,
                              params={"code": "duplicate-team", "state": state}, follow_redirects=False)
    assert response.status_code == 409
    assert client.get(f"/api/workspaces/{second_workspace}/slack",
                      headers=second_headers).json() == {"connected": False}


def test_slack_message_action_uses_connected_workspace_token(client, monkeypatch):
    configure_slack(monkeypatch)
    _, workspace_id, headers = create_workspace(client, f"slack-action-{uuid.uuid4()}@example.test")
    import app.main as main
    import app.slack as slack
    import app.store as store

    monkeypatch.setattr(main, "DEMO_MODE", False)
    monkeypatch.setattr(store, "DEMO_MODE", False)
    steps = [{"name": "announce", "action": "slack_message",
              "payload": {"channel": "C123TEST", "text": "Build finished."}}]
    missing = client.post("/api/workflow-definitions", headers={**headers, "Idempotency-Key": "slack:no-install"},
                          json={"title": "Slack notice", "steps": steps})
    assert missing.status_code == 422 and "active Slack App connection" in missing.text
    connect_slack(client, monkeypatch, headers, grant("T" + uuid.uuid4().hex[:12].upper()))
    definition = client.post("/api/workflow-definitions", headers={**headers, "Idempotency-Key": "slack:action"},
                            json={"title": "Slack notice", "steps": steps})
    assert definition.status_code == 201, definition.text

    calls = []
    monkeypatch.setattr(slack, "post_message", lambda payload, token, *, timeout_seconds, step_results: (
        calls.append((payload, token, timeout_seconds, step_results))
        or {"channel": payload["channel"], "message_ts": "1710000000.000100"}
    ))
    run = client.post(f"/api/workflow-definitions/{definition.json()['id']}/runs",
                      headers={**headers, "Idempotency-Key": "slack:action:run"})
    assert run.status_code == 202, run.text
    from tests.conftest import drive_run, get_run
    drive_run(client, workspace_id, worker_id="slack-message-test-worker")
    completed = get_run(client, run.json()["id"], workspace_id)
    assert completed["status"] == "completed"
    assert completed["side_effects"][0]["result"] == {
        "channel": "C123TEST", "message_ts": "1710000000.000100",
    }
    assert calls == [(steps[0]["payload"], BOT_TOKEN, 0.6, {})]
    assert BOT_TOKEN not in json.dumps(completed, default=str)


def test_slack_message_can_use_an_earlier_http_result(client, monkeypatch):
    configure_slack(monkeypatch)
    monkeypatch.setenv("RELAYCORE_HTTP_ALLOWED_HOSTS", "hooks.example.com")
    _, workspace_id, headers = create_workspace(client, f"slack-result-{uuid.uuid4()}@example.test")
    credential_secret = "http-bearer-" + "x" * 24
    credential = client.post(
        f"/api/workspaces/{workspace_id}/credentials",
        headers={**headers, "Idempotency-Key": f"slack-result:credential:{uuid.uuid4()}"},
        json={"provider": "http", "name": "build service", "secret": credential_secret,
              "allowed_host": "hooks.example.com"},
    )
    assert credential.status_code == 201, credential.text
    connect_slack(client, monkeypatch, headers, grant("T" + uuid.uuid4().hex[:12].upper()))

    import app.http_action as http_action
    import app.main as main
    import app.slack as slack
    import app.store as store

    monkeypatch.setattr(main, "DEMO_MODE", False)
    monkeypatch.setattr(store, "DEMO_MODE", False)
    steps = [
        {"name": "check build", "action": "http", "payload": {
            "method": "GET", "url": "https://hooks.example.com/v1/status",
            "credential_id": credential.json()["id"],
        }},
        {"name": "announce build", "action": "slack_message", "payload": {
            "channel": "C123TEST", "text": {"$step": {"index": 0, "pointer": "/body/summary"}},
        }},
    ]
    definition = client.post(
        "/api/workflow-definitions",
        headers={**headers, "Idempotency-Key": f"slack-result:workflow:{uuid.uuid4()}"},
        json={"title": "Announce build status", "steps": steps},
    )
    assert definition.status_code == 201, definition.text

    http_result = {"status_code": 200, "response_bytes": 0, "content_type": "application/json",
                   "body": {"summary": "Production healthy"}}
    monkeypatch.setattr(http_action, "execute_http_action", lambda *_args, **_kwargs: http_result)
    calls = []

    def post(payload, token, *, timeout_seconds, step_results):
        calls.append((payload, token, timeout_seconds, step_results))
        return {"channel": payload["channel"], "message_ts": "1710000000.000100"}

    monkeypatch.setattr(slack, "post_message", post)
    run = client.post(
        f"/api/workflow-definitions/{definition.json()['id']}/runs",
        headers={**headers, "Idempotency-Key": f"slack-result:run:{uuid.uuid4()}"},
    )
    assert run.status_code == 202, run.text
    from tests.conftest import drive_run, get_run
    drive_run(client, workspace_id, worker_id="slack-result-reference-worker")
    completed = get_run(client, run.json()["id"], workspace_id)

    assert completed["status"] == "completed"
    assert calls == [(steps[1]["payload"], BOT_TOKEN, 0.6, {0: http_result})]
    assert completed["side_effects"][1]["result"]["channel"] == "C123TEST"
    assert BOT_TOKEN not in json.dumps(completed, default=str)
    assert credential_secret not in json.dumps(completed, default=str)


def test_slack_result_references_must_point_to_an_earlier_http_step(monkeypatch):
    from pydantic import ValidationError

    from app.models import WorkflowRequest

    monkeypatch.setenv("RELAYCORE_HTTP_ALLOWED_HOSTS", "hooks.example.com")
    http = {"name": "fetch", "action": "http", "payload": {
        "method": "GET", "url": "https://hooks.example.com/status", "credential_id": str(uuid.uuid4()),
    }}

    def slack_step(index):
        return {"name": "announce", "action": "slack_message", "payload": {
            "channel": "C123TEST", "text": {"$step": {"index": index, "pointer": "/body/summary"}},
        }}

    WorkflowRequest.model_validate({"title": "valid chain", "steps": [http, slack_step(0)]})

    for steps, message in (([http, slack_step(1)], "earlier workflow step"),
                           ([slack_step(1), http], "earlier workflow step"),
                           ([{"name": "record", "action": "record"}, slack_step(0)], "earlier HTTP action")):
        with pytest.raises(ValidationError, match=message):
            WorkflowRequest.model_validate({"title": "invalid chain", "steps": steps})

    with pytest.raises(ValidationError, match="Slack message text"):
        WorkflowRequest.model_validate({"title": "event text", "steps": [
            {"name": "announce", "action": "slack_message", "payload": {
                "channel": "C123TEST", "text": {"$event": "/type"},
            }},
        ]})


def test_slack_message_api_is_bounded_and_honors_rate_limit(monkeypatch):
    import app.slack as slack
    from app.http_action import PermanentActionError, RetryableActionError

    class Response:
        def __init__(self, body: bytes, status: int, headers=None):
            self.body = body
            self.status = status
            self.headers = headers or {}

        def read(self, size):
            chunk, self.body = self.body[:size], self.body[size:]
            return chunk

    class Socket:
        def settimeout(self, _timeout):
            pass

    class Connection:
        def __init__(self, response):
            self.response = response
            self.sock = Socket()
            self.request_args = None
            self.closed = False

        def request(self, method, path, *, body, headers):
            self.request_args = method, path, body, headers

        def getresponse(self):
            return self.response

        def close(self):
            self.closed = True

    payload = {"channel": "C123TEST", "text": "Build finished."}
    calls = []
    responses = iter([
        Response(b'{"ok":true,"channel":"C123TEST","ts":"1710000000.000100"}', 200),
        Response(b'{"ok":true,"channel":"C123TEST","ts":"1710000000.000100"}', 200),
        Response(b"{}", 429, {"Retry-After": "2"}),
        Response(b"{}", 429, {}),
        Response(b"{}", 500, {}),
        Response(b"{}", 302, {}),
        Response(b"x" * (slack.MAX_MESSAGE_RESPONSE_BYTES + 1), 200),
    ])

    def resolve(host, port, timeout):
        calls.append(("dns", host, port, timeout))
        time.sleep(0.02)
        return ["93.184.216.34"]

    def connect(host, port, address, timeout):
        connection = Connection(next(responses))
        calls.append(("connect", host, port, address, timeout, connection))
        return connection

    monkeypatch.setattr(slack, "_resolve_public_addresses", resolve)
    monkeypatch.setattr(slack, "_PinnedHTTPSConnection", connect)
    assert post_message(payload, BOT_TOKEN) == {"channel": "C123TEST", "message_ts": "1710000000.000100"}
    assert calls[0] == ("dns", "slack.com", 443, 1)
    assert 0 < calls[1][4] < 1
    connection = calls[1][5]
    method, path, body, headers = connection.request_args
    assert (method, path) == ("POST", "/api/chat.postMessage")
    assert headers["Authorization"] == f"Bearer {BOT_TOKEN}"
    assert json.loads(body) == payload
    assert connection.closed

    mapped = {"channel": "C123TEST", "text": {"$step": {"index": 0, "pointer": "/body/summary"}}}
    assert post_message(mapped, BOT_TOKEN, step_results={0: {"body": {"summary": "Production healthy"}}}) == {
        "channel": "C123TEST", "message_ts": "1710000000.000100",
    }
    assert json.loads(calls[-1][5].request_args[2]) == {"channel": "C123TEST", "text": "Production healthy"}
    connection_count = len(calls)
    with pytest.raises(PermanentActionError, match="was not present"):
        post_message(mapped, BOT_TOKEN)
    with pytest.raises(PermanentActionError, match="Resolved Slack message text"):
        post_message(mapped, BOT_TOKEN, step_results={0: {"body": {"summary": {"unsafe": True}}}})
    with pytest.raises(PermanentActionError, match="Resolved Slack message text"):
        post_message(mapped, BOT_TOKEN, step_results={0: {"body": {"summary": "x" * 4001}}})
    assert len(calls) == connection_count

    with pytest.raises(RetryableActionError, match="rate limited") as error:
        post_message(payload, BOT_TOKEN)
    assert error.value.retry_after == 2

    with pytest.raises(RetryableActionError, match="rate limited") as error:
        post_message(payload, BOT_TOKEN)
    assert error.value.retry_after is None

    with pytest.raises(RetryableActionError, match="status 500"):
        post_message(payload, BOT_TOKEN)

    with pytest.raises(PermanentActionError, match="redirects"):
        post_message(payload, BOT_TOKEN)

    with pytest.raises(PermanentActionError, match="size limit"):
        post_message(payload, BOT_TOKEN)

    for invalid in ({"channel": "#general", "text": "hi"},
                    {"channel": "C123TEST", "text": "\x00"},
                    {"channel": "C123TEST", "text": "hello", "extra": True}):
        with pytest.raises(ValueError):
            validate_message_step(invalid)
