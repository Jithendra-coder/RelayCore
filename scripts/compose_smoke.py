from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from typing import Any
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
API_KEY = "relaycore-compose-smoke-admin"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request_json(
    base_url: str, path: str, *, method: str = "GET", body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=3) as response:
            payload = json.load(response)
            if not isinstance(payload, dict):
                raise RuntimeError(f"Compose smoke request returned a non-object: {method} {path}")
            return payload
    except HTTPError as exc:
        raise RuntimeError(f"Compose smoke request returned HTTP {exc.code}: {method} {path}") from exc


def _dashboard_run(base_url: str) -> str:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('relaycore-key', {json.dumps(API_KEY)});")
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(base_url, wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="Workflow operations")).to_be_visible()
            expect(page.locator("#healthText")).to_have_text("Operational", timeout=15_000)
            expect(page.locator("#modeTag")).to_have_text("SANDBOX MODE")

            run_button = page.get_by_role("button", name="Run demo")
            run_button.focus()
            page.keyboard.press("Enter")
            run = page.locator("#runs .run.selected")
            expect(run).to_be_visible(timeout=10_000)
            run_id = run.get_attribute("data-run")
            if not run_id:
                raise RuntimeError("Dashboard did not select the newly created workflow run.")
            expect(run.locator(".status")).to_have_text("completed", timeout=40_000)
            if page_errors:
                raise RuntimeError("Dashboard raised a JavaScript error: " + "; ".join(page_errors))
            return run_id
        finally:
            browser.close()


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

        run_id = _dashboard_run(base_url)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            run = _request_json(base_url, f"/api/workflows/{run_id}")
            if run.get("status") == "completed":
                effects = len(run.get("side_effects", []))
                if effects != 4:
                    raise RuntimeError(f"Compose workflow persisted {effects} side-effect rows; expected 4.")
                print("Browser smoke passed: dashboard keyboard action and separate worker completed four durable steps.")
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
