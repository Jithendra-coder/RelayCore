from __future__ import annotations

import ipaddress
import json
import math
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


class RelayCoreAPIError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"RelayCore returned HTTP {status_code}: {detail}")


class RelayCoreConnectionError(Exception):
    """The RelayCore endpoint could not be reached or returned invalid JSON."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


@dataclass(frozen=True)
class Workspace:
    id: str
    name: str
    role: str

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Workspace:
        return cls(id=str(value["id"]), name=str(value["name"]), role=str(value["role"]))


@dataclass(frozen=True)
class WorkflowDefinition:
    id: str
    title: str
    status: str
    current_version: int
    created_at: str | None = None
    updated_at: str | None = None
    versions: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> WorkflowDefinition:
        return cls(
            id=str(value["id"]), title=str(value["title"]), status=str(value["status"]),
            current_version=int(value["current_version"]), created_at=value.get("created_at"),
            updated_at=value.get("updated_at"), versions=tuple(value.get("versions", ())),
        )


@dataclass(frozen=True)
class WorkflowAccepted:
    id: str
    status: str
    created: bool = True
    version: int | None = None
    workflow_version_id: str | None = None

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> WorkflowAccepted:
        return cls(id=str(value["id"]), status=str(value["status"]), created=bool(value.get("created", True)),
                   version=int(value["version"]) if value.get("version") is not None else None,
                   workflow_version_id=value.get("workflow_version_id"))


@dataclass(frozen=True)
class WorkflowVersionCreated:
    id: str
    version: int
    created: bool
    version_id: str | None = None

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> WorkflowVersionCreated:
        return cls(id=str(value["id"]), version=int(value["version"]), created=bool(value["created"]),
                   version_id=value.get("version_id"))


@dataclass(frozen=True)
class RunCancelled:
    id: str
    status: str

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> RunCancelled:
        return cls(id=str(value["id"]), status=str(value["status"]))


@dataclass(frozen=True)
class WorkflowRun:
    id: str
    title: str
    status: str
    step_index: int | None = None
    task_status: str | None = None
    created_at: str | None = None
    finished_at: str | None = None
    details: Mapping[str, Any] | None = None

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> WorkflowRun:
        return cls(
            id=str(value["id"]), title=str(value.get("title", "")), status=str(value["status"]),
            step_index=int(value["step_index"]) if value.get("step_index") is not None else None,
            task_status=value.get("task_status"), created_at=value.get("created_at"),
            finished_at=value.get("finished_at"), details=value,
        )


class RelayCoreClient:
    """Synchronous client for one workspace, using an expiring API token."""

    def __init__(self, base_url: str, token: str, workspace_id: str, *, timeout: float = 10.0):
        parsed = urllib.parse.urlsplit(base_url)
        host = parsed.hostname or ""
        is_loopback = host.lower() == "localhost"
        try:
            is_loopback = is_loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if (parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password
                or parsed.query or parsed.fragment or (parsed.scheme != "https" and not is_loopback)):
            raise ValueError("Use an HTTPS RelayCore URL (HTTP is allowed only on loopback).")
        if not token or not workspace_id:
            raise ValueError("An API token and workspace ID are required.")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout must be a finite positive number.")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.workspace_id = workspace_id
        self.timeout = timeout

    def _request(self, method: str, path: str, *, body: Mapping[str, Any] | None = None,
                 idempotency_key: str | None = None) -> Any:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "X-Workspace-ID": self.workspace_id,
        }
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=self.timeout) as response:
                if response.status == 204:
                    return None
                payload = response.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("detail", "Request rejected.")
            except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
                detail = "Request rejected."
            if not isinstance(detail, str):
                detail = "Request rejected."
            raise RelayCoreAPIError(exc.code, detail[:500]) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RelayCoreConnectionError("Could not connect to the RelayCore API.") from exc
        try:
            return json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RelayCoreConnectionError("RelayCore returned an invalid JSON response.") from exc

    @staticmethod
    def _id_path(value: str) -> str:
        return urllib.parse.quote(value, safe="")

    def list_workspaces(self) -> list[Workspace]:
        return [Workspace.from_json(row) for row in self._request("GET", "/api/workspaces")]

    def list_workflow_definitions(self) -> list[WorkflowDefinition]:
        return [WorkflowDefinition.from_json(row) for row in self._request("GET", "/api/workflow-definitions")]

    def get_workflow_definition(self, workflow_id: str) -> WorkflowDefinition:
        path = "/api/workflow-definitions/" + self._id_path(workflow_id)
        return WorkflowDefinition.from_json(self._request("GET", path))

    def create_workflow_definition(
        self, title: str, steps: Sequence[Mapping[str, Any]], *, trigger: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> WorkflowVersionCreated:
        body: dict[str, Any] = {"title": title, "steps": list(steps)}
        if trigger is not None:
            body["trigger"] = trigger
        return WorkflowVersionCreated.from_json(self._request(
            "POST", "/api/workflow-definitions", body=body, idempotency_key=idempotency_key or str(uuid.uuid4()),
        ))

    def start_workflow(self, workflow_id: str, *, idempotency_key: str | None = None) -> WorkflowAccepted:
        path = "/api/workflow-definitions/" + self._id_path(workflow_id) + "/runs"
        return WorkflowAccepted.from_json(self._request(
            "POST", path, idempotency_key=idempotency_key or str(uuid.uuid4()),
        ))

    def list_runs(self) -> list[WorkflowRun]:
        return [WorkflowRun.from_json(row) for row in self._request("GET", "/api/workflows")]

    def get_run(self, run_id: str) -> WorkflowRun:
        path = "/api/workflows/" + self._id_path(run_id)
        return WorkflowRun.from_json(self._request("GET", path))

    def cancel_run(self, run_id: str) -> RunCancelled:
        path = "/api/workflows/" + self._id_path(run_id) + "/cancel"
        return RunCancelled.from_json(self._request("POST", path))
