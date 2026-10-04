from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from tests.conftest import ADMIN, make_workflow


def test_queued_workflow_survives_api_process_death_and_restart(client):
    response = make_workflow(client, title="persist across API restart")
    run_id = response.json()["id"]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "RELAYCORE_WORKERS": "0", "RELAYCORE_DEMO_MODE": "1"}
    api = f"http://127.0.0.1:{port}"

    def start(log):
        return subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        )

    def wait_ready(process):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and process.poll() is None:
            try:
                if httpx.get(api + "/healthz", timeout=0.5).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise AssertionError("API subprocess did not become healthy")

    with tempfile.TemporaryFile(mode="w+t") as first_log, tempfile.TemporaryFile(mode="w+t") as second_log:
        first = start(first_log)
        try:
            wait_ready(first)
            run = httpx.get(api + f"/api/workflows/{run_id}", headers=ADMIN, timeout=2)
            assert run.status_code == 200 and run.json()["status"] == "queued"
        finally:
            first.kill()
            first.wait(timeout=5)

        second = start(second_log)
        try:
            wait_ready(second)
            run = httpx.get(api + f"/api/workflows/{run_id}", headers=ADMIN, timeout=2)
            assert run.status_code == 200 and run.json()["status"] == "queued"
            assert run.json()["events"][0]["request_id"]
            assert run.json()["events"][0]["sequence"]
        finally:
            second.terminate()
            second.wait(timeout=5)
