from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
API_KEY = "relaycore-compose-smoke-admin"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request_json(base_url: str, path: str, *, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=3) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Compose smoke request returned HTTP {exc.code}: {method} {path}") from exc


def _environment(port: int, database_password: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({
        "DATABASE_URL": f"postgresql://relaycore:{database_password}@db:5432/relaycore",
        "POSTGRES_PASSWORD": database_password,
        "RELAYCORE_API_KEYS": json.dumps({
            API_KEY: {"tenant_id": "compose-smoke", "role": "admin"},
        }),
        "RELAYCORE_DEMO_MODE": "1",
        "RELAYCORE_WORKERS": "0",
        "RELAYCORE_LEASE_SECONDS": "4",
        "RELAYCORE_PORT": str(port),
        "RELAYCORE_HTTP_ALLOWED_HOSTS": "",
        "RELAYCORE_SECRET_ENCRYPTION_KEY": "",
    })
    for name in (
        "RELAYCORE_OIDC_ISSUER", "RELAYCORE_OIDC_CLIENT_ID", "RELAYCORE_OIDC_CLIENT_SECRET",
        "RELAYCORE_OIDC_REDIRECT_URI", "RELAYCORE_OIDC_STATE_SECRET", "RELAYCORE_OIDC_DISCOVERY_URL",
        "RELAYCORE_GITHUB_APP_SLUG", "RELAYCORE_GITHUB_APP_ID", "RELAYCORE_GITHUB_CLIENT_ID",
        "RELAYCORE_GITHUB_CLIENT_SECRET", "RELAYCORE_GITHUB_PRIVATE_KEY_B64", "RELAYCORE_GITHUB_WEBHOOK_SECRET",
        "RELAYCORE_GITHUB_SETUP_URL", "RELAYCORE_GITHUB_CALLBACK_URL", "RELAYCORE_SLACK_CLIENT_ID",
        "RELAYCORE_SLACK_CLIENT_SECRET", "RELAYCORE_SLACK_APP_ID", "RELAYCORE_SLACK_SIGNING_SECRET",
        "RELAYCORE_SLACK_CALLBACK_URL",
    ):
        environment[name] = ""
    return environment


def main() -> None:
    if not shutil.which("docker"):
        raise RuntimeError("Docker is required for the disposable Compose smoke run.")

    project = f"relaycore-smoke-{uuid.uuid4().hex[:10]}"
    compose = ["docker", "compose", "--project-name", project,
               "-f", "compose.yaml", "-f", "compose.workers.yaml"]
    environment = _environment(_free_port(), uuid.uuid4().hex)
    base_url = f"http://127.0.0.1:{environment['RELAYCORE_PORT']}"
    try:
        subprocess.run(compose + ["up", "--detach", "--build", "--wait", "--wait-timeout", "90"],
                       cwd=ROOT, env=environment, check=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if _request_json(base_url, "/healthz").get("status") == "ok":
                    break
            except (HTTPError, URLError, TimeoutError):
                time.sleep(0.25)
        else:
            raise RuntimeError("Compose API did not become reachable after its health check passed.")

        run_id = _request_json(base_url, "/api/demo/start", method="POST").get("id")
        if not run_id:
            raise RuntimeError("Demo Mode did not return a workflow run ID.")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            run = _request_json(base_url, f"/api/workflows/{run_id}")
            if run.get("status") == "completed":
                effects = len(run.get("side_effects", []))
                if effects != 4:
                    raise RuntimeError(f"Compose workflow completed with {effects} effects; expected 4.")
                print("Compose smoke passed: separate API and worker completed one four-step durable workflow.")
                return
            if run.get("status") in {"failed", "cancelled"}:
                raise RuntimeError(f"Compose workflow ended with status {run.get('status')}.")
            time.sleep(0.4)
        raise RuntimeError("Compose workflow did not complete within 30 seconds.")
    finally:
        subprocess.run(compose + ["down", "--volumes", "--remove-orphans"],
                       cwd=ROOT, env=environment, check=True)


if __name__ == "__main__":
    main()
