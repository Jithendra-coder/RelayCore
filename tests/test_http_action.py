from __future__ import annotations

import socket
import ssl

import pytest

import app.http_action as http_action

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
    def settimeout(self, _timeout):
        pass


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
                        lambda _host, _port: ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
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
