from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from relaycore_sdk import (
    RelayCoreAPIError,
    RelayCoreClient,
    RelayCoreConnectionError,
    WorkflowVersionCreated,
)
from relaycore_sdk.cli import main


def client():
    return RelayCoreClient("https://relay.example.test", "rca_test-token", "workspace-1")


def mocked_response(payload: object, status: int = 200):
    opener = MagicMock()
    response = MagicMock()
    response.status = status
    response.read.return_value = json.dumps(payload).encode()
    opener.open.return_value.__enter__.return_value = response
    return patch("urllib.request.build_opener", return_value=opener), opener


def test_sdk_sets_workspace_auth_and_returns_typed_models():
    patcher, opener = mocked_response([{"id": "ws-1", "name": "Builds", "role": "OWNER"}])
    with patcher:
        workspaces = client().list_workspaces()
        request = opener.open.call_args.args[0]
    assert workspaces[0].name == "Builds"
    assert request.get_header("Authorization") == "Bearer rca_test-token"
    assert request.get_header("X-workspace-id") == "workspace-1"
    assert request.full_url == "https://relay.example.test/api/workspaces"


def test_sdk_sends_idempotency_key_for_workflow_run():
    patcher, opener = mocked_response({"id": "run-1", "status": "queued", "created": True})
    with patcher:
        result = client().start_workflow("workflow/one", idempotency_key="stable-key")
        request = opener.open.call_args.args[0]
    assert result.id == "run-1"
    assert request.full_url.endswith("/api/workflow-definitions/workflow%2Fone/runs")
    assert request.get_header("Idempotency-key") == "stable-key"


def test_sdk_creates_a_versioned_workflow_as_json():
    patcher, opener = mocked_response({"id": "workflow-1", "version_id": "version-1",
                                       "version": 1, "created": True}, 201)
    with patcher:
        result = client().create_workflow_definition("Release", [{"name": "Notify", "action": "slack_message",
                                                                   "payload": {"channel": "C1", "text": "Ready"}}],
                                                     idempotency_key="release:v1")
        request = opener.open.call_args.args[0]
    assert result == WorkflowVersionCreated("workflow-1", 1, True, "version-1")
    assert json.loads(request.data) == {
        "title": "Release",
        "steps": [{"name": "Notify", "action": "slack_message", "payload": {"channel": "C1", "text": "Ready"}}],
    }
    assert request.get_header("Idempotency-key") == "release:v1"


def test_sdk_surfaces_api_errors_without_echoing_request_body():
    error = HTTPError("https://relay.example.test", 401, "Unauthorized", {},
                      io.BytesIO(b'{"detail":"Bearer token is not valid."}'))
    with patch("urllib.request.build_opener") as build_opener, pytest.raises(RelayCoreAPIError) as raised:
        build_opener.return_value.open.side_effect = error
        client().list_workspaces()
    assert raised.value.status_code == 401
    assert str(raised.value) == "RelayCore returned HTTP 401: Bearer token is not valid."


def test_sdk_translates_transport_errors():
    with patch("urllib.request.build_opener", side_effect=URLError("offline")), \
            pytest.raises(RelayCoreConnectionError):
        client().list_workspaces()


def test_sdk_does_not_follow_redirects_with_a_bearer_token():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/redirect-target")
            self.end_headers()

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        sdk_client = RelayCoreClient(f"http://127.0.0.1:{server.server_port}", "rca_test-token", "workspace-1")
        with pytest.raises(RelayCoreAPIError) as raised:
            sdk_client.list_workspaces()
        assert raised.value.status_code == 302
        assert seen == ["/api/workspaces"]
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_sdk_requires_https_except_loopback_and_valid_timeout():
    with pytest.raises(ValueError, match="HTTPS"):
        RelayCoreClient("http://relay.example.test", "token", "ws")
    with pytest.raises(ValueError, match="Timeout"):
        RelayCoreClient("http://127.0.0.1:8000", "token", "ws", timeout=0)
    assert RelayCoreClient("http://localhost:8000", "token", "ws")


def test_cli_requires_token_without_accepting_it_as_an_argument(monkeypatch, capsys):
    monkeypatch.delenv("RELAYCORE_API_TOKEN", raising=False)
    monkeypatch.setenv("RELAYCORE_URL", "https://relay.example.test")
    monkeypatch.setenv("RELAYCORE_WORKSPACE_ID", "workspace-1")
    assert main(["workspaces", "list"]) == 2
    assert "RELAYCORE_API_TOKEN" in capsys.readouterr().err


def test_cli_reads_workflow_json_and_uses_stable_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("RELAYCORE_API_TOKEN", "rca_test-token")
    monkeypatch.setenv("RELAYCORE_URL", "https://relay.example.test")
    monkeypatch.setenv("RELAYCORE_WORKSPACE_ID", "workspace-1")
    workflow_file = tmp_path / "workflow.json"
    workflow_file.write_text(json.dumps({"title": "Release", "steps": [{"name": "Notify"}]}), encoding="utf-8")
    with patch("relaycore_sdk.cli.RelayCoreClient") as client_factory:
        client_factory.return_value.create_workflow_definition.return_value = WorkflowVersionCreated(
            "workflow-1", 1, True, "version-1")
        assert main(["workflow", "create", str(workflow_file), "--idempotency-key", "release:v1"]) == 0
    client_factory.return_value.create_workflow_definition.assert_called_once_with(
        "Release", [{"name": "Notify"}], trigger=None, idempotency_key="release:v1")
    assert json.loads(capsys.readouterr().out)["id"] == "workflow-1"
