from __future__ import annotations

import socket
import ssl
import threading
import time

import pytest

import app.http_action as http_action
from app.models import WorkflowRequest

PinnedHTTPSConnection = http_action._PinnedHTTPSConnection


def payload(**overrides):
    return {
        "method": "POST",
        "url": "https://hooks.example.com/v1/events?source=relaycore",
        "credential_id": "55c1be96-88c5-4bc4-841e-5c9dc6e8b229",
        "headers": {"X-Trace-ID": "trace-1"},
        "body": {"event": "build.completed"},
        "timeout_seconds": 0.5,
        **overrides,
    }


class FakeSocket:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, _timeout):
        self.timeouts.append(_timeout)


class FakeResponse:
    def __init__(self, status=200, body=b'{"ok":true}'):
        self.status = status
        self.body = body

    def read(self, size):
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk

    def getheader(self, name, default=""):
        return "application/json" if name.lower() == "content-type" else default


class FakeConnection:
    def __init__(self, host, port, address, timeout):
        self.host, self.port, self.address, self.timeout = host, port, address, timeout
        self.sock = FakeSocket()
        self.closed = False
        self.args = None
        self.response = FakeResponse()

    def request(self, method, target, *, body, headers):
        self.args = method, target, body, headers

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def fake_transport(monkeypatch, *, status=200, body=b'{"ok":true}'):
    connections = []

    def connect(host, port, address, timeout):
        connection = FakeConnection(host, port, address, timeout)
        connection.response = FakeResponse(status, body)
        connections.append(connection)
        return connection

    monkeypatch.setenv("RELAYCORE_HTTP_ALLOWED_HOSTS", "hooks.example.com")
    monkeypatch.setattr(http_action, "_resolve_public_addresses",
                        lambda _host, _port, _timeout: ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
    monkeypatch.setattr(http_action, "_PinnedHTTPSConnection", connect)
    return connections


def test_http_targets_require_allowlisted_https_dns_hosts(monkeypatch):
    monkeypatch.setenv("RELAYCORE_HTTP_ALLOWED_HOSTS", "hooks.example.com")
    assert http_action.validate_http_target("https://hooks.example.com/v1") == (
        "hooks.example.com", 443, "/v1"
    )
    for url in (
        "http://hooks.example.com/", "https://other.example.com/", "https://127.0.0.1/",
        "https://user:pass@hooks.example.com/", "https://hooks.example.com:8443/",
        "https://hooks.example.com/?access_token=stored-secret", "https://hooks.example.com\\@evil.example/",
    ):
        with pytest.raises(ValueError):
            http_action.validate_http_target(url)


def test_http_step_rejects_auth_headers_and_private_dns(monkeypatch):
    monkeypatch.setenv("RELAYCORE_HTTP_ALLOWED_HOSTS", "hooks.example.com")
    for headers in ({"Authorization": "Bearer leaked"}, {"X-API-Key": "leaked"}, {"X-Trace": "bad\nvalue"}):
        with pytest.raises(ValueError, match="header"):
            http_action.validate_http_step(payload(headers=headers))

    monkeypatch.setattr(http_action.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ])
    with pytest.raises(http_action.PermanentActionError, match="non-public"):
        http_action.execute_http_action(payload(), "workspace-token-123456", "run:0", "hooks.example.com")


def test_http_dns_timeout_is_retryable_before_opening_a_connection(monkeypatch):
    monkeypatch.setenv("RELAYCORE_HTTP_ALLOWED_HOSTS", "hooks.example.com")
    started, release = threading.Event(), threading.Event()

    def stalled_resolver(*_args, **_kwargs):
        started.set()
        release.wait(2)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", stalled_resolver)
    monkeypatch.setattr(http_action, "_PinnedHTTPSConnection",
                        lambda *_args: pytest.fail("A timed-out DNS lookup must not open a connection."))
    before = time.monotonic()
    try:
        with pytest.raises(http_action.RetryableActionError, match="DNS resolution timed out"):
            http_action.execute_http_action(payload(timeout_seconds=0.1), "workspace-token-123456",
                                            "run:0", "hooks.example.com")
        assert started.is_set()
        assert time.monotonic() - before < 0.5
    finally:
        release.set()


def test_http_dns_error_is_retryable(monkeypatch):
    monkeypatch.setenv("RELAYCORE_HTTP_ALLOWED_HOSTS", "hooks.example.com")

    def failed_resolver(*_args, **_kwargs):
        raise socket.gaierror("temporary resolver failure")

    monkeypatch.setattr(socket, "getaddrinfo", failed_resolver)
    with pytest.raises(http_action.RetryableActionError, match="DNS resolution failed"):
        http_action.execute_http_action(payload(), "workspace-token-123456", "run:0", "hooks.example.com")


def test_http_credential_cannot_be_sent_to_a_different_allowlisted_host(monkeypatch):
    monkeypatch.setenv("RELAYCORE_HTTP_ALLOWED_HOSTS", "hooks.example.com,other.example.com")
    monkeypatch.setattr(http_action, "_resolve_public_addresses",
                        lambda *_args: pytest.fail("Host mismatch must be checked before DNS or network access."))
    with pytest.raises(http_action.PermanentActionError, match="bound host"):
        http_action.execute_http_action(payload(), "workspace-token-123456", "run:0", "other.example.com")


def test_http_action_pins_public_dns_and_redacts_response_secrets(monkeypatch):
    connections = fake_transport(
        monkeypatch,
        body=b'{"ok":true,"access_token":"response-secret","metadata":{"api-key":"another-secret"},'
             b'"message":"provider echoed workspace-token-123456"}',
    )
    result = http_action.execute_http_action(payload(), "workspace-token-123456", "run-id:step-0", "hooks.example.com")
    connection = connections[0]
    method, target, body, headers = connection.args
    assert (connection.host, connection.port, connection.address) == (
        "hooks.example.com", 443, "93.184.216.34"
    )
    assert method == "POST" and target == "/v1/events?source=relaycore"
    assert body == b'{"event":"build.completed"}'
    assert headers["Authorization"] == "Bearer workspace-token-123456"
    assert headers["Idempotency-Key"] == "run-id:step-0"
    assert result["body"] == {
        "ok": True,
        "access_token": "[redacted]",
        "metadata": {"api-key": "[redacted]"},
        "message": "provider echoed [redacted]",
    }
    assert connection.closed


def test_http_action_uses_one_timeout_budget_for_dns_and_https(monkeypatch):
    connections = fake_transport(monkeypatch)

    def slow_resolver(_host, _port, _timeout):
        time.sleep(0.05)
        return ["93.184.216.34"]

    monkeypatch.setattr(http_action, "_resolve_public_addresses", slow_resolver)
    http_action.execute_http_action(payload(timeout_seconds=0.5), "workspace-token-123456",
                                   "run:0", "hooks.example.com")
    connection = connections[0]
    assert 0 < connection.timeout < 0.45
    assert connection.sock.timeouts and all(0 < timeout < 0.5 for timeout in connection.sock.timeouts)


def test_http_action_resolves_limited_event_references_in_json_body(monkeypatch):
    connections = fake_transport(monkeypatch)
    request = payload(body={
        "event": {"$event": "/type"},
        "repository": {"name": {"$event": "/repository/full_name"}},
        "label": {"$event": "/labels/0/name"},
        "escaped/key": {"$event": "/metadata/a~1b"},
    })
    event = {"type": "push", "repository": {"full_name": "example/service"},
             "labels": [{"name": "critical"}], "metadata": {"a/b": "ok"}}
    http_action.execute_http_action(request, "workspace-token-123456", "run:0", "hooks.example.com", event)
    assert connections[0].args[2] == (
        b'{"event":"push","repository":{"name":"example/service"},"label":"critical",'
        b'"escaped/key":"ok"}'
    )


def test_http_action_resolves_prior_step_response_references(monkeypatch):
    connections = fake_transport(monkeypatch)
    request = payload(body={
        "task_id": {"$step": {"index": 0, "pointer": "/body/task/id"}},
        "status": "ready",
    })
    previous_results = {0: {"status_code": 201, "body": {"task": {"id": "task-42"}}}}

    http_action.execute_http_action(request, "workspace-token-123456", "run:1", "hooks.example.com",
                                    step_results=previous_results)

    assert connections[0].args[2] == b'{"task_id":"task-42","status":"ready"}'


@pytest.mark.parametrize("reference", [
    {"$event": "type"}, {"$event": "/type~2name"}, {"$event": "/type", "fixed": True},
])
def test_http_action_rejects_invalid_event_references(monkeypatch, reference):
    monkeypatch.setenv("RELAYCORE_HTTP_ALLOWED_HOSTS", "hooks.example.com")
    with pytest.raises(ValueError, match="event references"):
        http_action.validate_http_step(payload(body={"value": reference}))


@pytest.mark.parametrize("reference", [
    {"$step": {"index": True, "pointer": "/body/id"}},
    {"$step": {"index": 0, "pointer": "body/id"}},
    {"$step": {"index": 0, "pointer": "/body/id~2"}},
    {"$step": {"index": 0, "pointer": "/body/id", "extra": True}},
])
def test_http_action_rejects_invalid_step_references(monkeypatch, reference):
    monkeypatch.setenv("RELAYCORE_HTTP_ALLOWED_HOSTS", "hooks.example.com")
    with pytest.raises(ValueError, match="step references"):
        http_action.validate_http_step(payload(body={"value": reference}))


def test_http_action_dead_letters_missing_or_oversized_step_mappings(monkeypatch):
    connections = fake_transport(monkeypatch)
    request = payload(body={"value": {"$step": {"index": 0, "pointer": "/body/id"}}})
    with pytest.raises(http_action.PermanentActionError, match="step result"):
        http_action.execute_http_action(request, "workspace-token-123456", "run:1", "hooks.example.com")
    with pytest.raises(http_action.PermanentActionError, match="4 KiB"):
        http_action.execute_http_action(request, "workspace-token-123456", "run:1", "hooks.example.com",
                                        step_results={0: {"body": {"id": "x" * 4096}}})
    assert connections == []


def test_workflow_step_references_require_an_earlier_http_action(monkeypatch):
    monkeypatch.setenv("RELAYCORE_HTTP_ALLOWED_HOSTS", "hooks.example.com")
    credential = "55c1be96-88c5-4bc4-841e-5c9dc6e8b229"

    def http_step(body):
        return {"name": "HTTP", "action": "http", "payload": {
            "method": "POST", "url": "https://hooks.example.com/events", "credential_id": credential,
            "body": body,
        }}

    reference = {"$step": {"index": 0, "pointer": "/body/id"}}
    workflow = WorkflowRequest(title="Map response", steps=[http_step({}), http_step({"id": reference})])
    assert len(workflow.steps) == 2

    with pytest.raises(ValueError, match="earlier workflow step"):
        WorkflowRequest(title="Forward reference", steps=[http_step({"id": reference}), http_step({})])
    with pytest.raises(ValueError, match="earlier workflow step"):
        WorkflowRequest(title="Self reference", steps=[http_step({"id": {"$step": {
            "index": 0, "pointer": "/body/id",
        }}})])
    with pytest.raises(ValueError, match="earlier HTTP action"):
        WorkflowRequest(title="Non-HTTP source", steps=[
            {"name": "Slack", "action": "slack_message", "payload": {"channel": "C12345678", "text": "ready"}},
            http_step({"id": reference}),
        ])


def test_http_action_dead_letters_missing_or_oversized_event_mappings(monkeypatch):
    connections = fake_transport(monkeypatch)
    with pytest.raises(http_action.PermanentActionError, match="not present"):
        http_action.execute_http_action(payload(body={"value": {"$event": "/missing"}}),
                                       "workspace-token-123456", "run:0", "hooks.example.com", {})
    assert connections == []
    with pytest.raises(http_action.PermanentActionError, match="4 KiB"):
        http_action.execute_http_action(payload(body={"value": {"$event": "/large"}}),
                                       "workspace-token-123456", "run:0", "hooks.example.com",
                                       {"large": "x" * 4096})
    assert connections == []


@pytest.mark.parametrize(("status", "error"), [
    (302, http_action.PermanentActionError),
    (401, http_action.PermanentActionError),
    (429, http_action.RetryableActionError),
    (503, http_action.RetryableActionError),
])
def test_http_statuses_are_classified_for_retry_without_following_redirects(monkeypatch, status, error):
    fake_transport(monkeypatch, status=status)
    with pytest.raises(error):
        http_action.execute_http_action(payload(), "workspace-token-123456", "run-id:step-0", "hooks.example.com")


def test_http_response_size_is_bounded_and_tls_verification_is_on(monkeypatch):
    fake_transport(monkeypatch, body=b"x" * (http_action.MAX_RESPONSE_BYTES + 1))
    with pytest.raises(http_action.PermanentActionError, match="64 KiB"):
        http_action.execute_http_action(payload(), "workspace-token-123456", "run-id:step-0", "hooks.example.com")
    connection = PinnedHTTPSConnection("hooks.example.com", 443, "93.184.216.34", 1)
    assert connection._context.verify_mode == ssl.CERT_REQUIRED
    assert connection._context.check_hostname
    connection.close()
