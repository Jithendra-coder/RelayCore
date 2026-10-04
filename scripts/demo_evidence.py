from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path


def call(base: str, token: str, path: str, *, method: str = "GET", body: dict | None = None,
         extra_headers: dict[str, str] | None = None):
    headers = {"Authorization": f"Bearer {token}", "X-Request-ID": f"evidence-{uuid.uuid4()}"}
    headers.update(extra_headers or {})
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            content = response.read()
            if not content:
                parsed = None
            elif "json" in response.headers.get("Content-Type", ""):
                parsed = json.loads(content)
            else:
                parsed = content.decode()
            return response.status, parsed, response.headers
    except urllib.error.HTTPError as exc:
        message = exc.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} returned {exc.code}: {message}") from exc


def wait_for(base: str, token: str, run_id: str, predicate, timeout: float = 30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, run, _ = call(base, token, f"/api/workflows/{run_id}")
        if predicate(run):
            return run
        time.sleep(0.12)
    raise TimeoutError(f"Workflow {run_id} did not meet the expected state in {timeout:.0f}s.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the RelayCore recovery and duplicate-event demo against a local Demo Mode API.")
    parser.add_argument("--url", default=os.environ.get("RELAYCORE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--key", default=os.environ.get("RELAYCORE_DEMO_KEY", "demo-key-change-me-32"))
    parser.add_argument("--output", type=Path, default=Path("docs/evidence/demo-run.json"))
    args = parser.parse_args()
    base, token = args.url.rstrip("/"), args.key
    started = time.perf_counter()

    _, health, _ = call(base, token, "/healthz")
    if health.get("status") != "ok":
        raise RuntimeError("API/database health check failed.")
    _, config, _ = call(base, token, "/api/config")
    if not config.get("demo_mode"):
        raise RuntimeError("Enable RELAYCORE_DEMO_MODE to run failure injection.")

    duplicate_key = f"evidence-{uuid.uuid4()}"
    _, duplicate, _ = call(base, token, "/api/demo/duplicates", method="POST", body={
        "event_key": duplicate_key, "order": "evidence-order", "amount": 25, "currency": "USD"
    })
    duplicate_run = wait_for(base, token, duplicate["workflow_id"], lambda run: run["status"] == "completed")
    if duplicate["received"] != 10 or len(duplicate_run["side_effects"]) != 3:
        raise AssertionError("Duplicate delivery did not resolve to 10 receipts and three logical step effects.")

    key = f"evidence:{uuid.uuid4()}"
    _, recovery, _ = call(base, token, "/api/workflows", method="POST", extra_headers={"Idempotency-Key": key}, body={
        "title": "Evidence run: kill worker during a leased step",
        "steps": [
            {"name": "Reserve inventory", "action": "record", "payload": {"order": "recovery-evidence"}},
            {"name": "Active lease failure point", "action": "sleep", "payload": {"seconds": 2.5}},
            {"name": "Charge once", "action": "charge", "payload": {"amount": 25, "currency": "USD"}},
            {"name": "Confirm shipment", "action": "record", "payload": {"order": "recovery-evidence"}},
        ],
    })
    run_id = recovery["id"]
    active = wait_for(base, token, run_id,
                      lambda run: run["task_status"] == "running" and run["step_index"] == 1)
    killed_worker = active["lease_owner"]
    _, killed, _ = call(base, token, f"/api/workers/{killed_worker}/kill", method="POST", body={})
    recovered = wait_for(base, token, run_id, lambda run: run["status"] == "completed", timeout=30)
    recovered_kinds = [event["kind"] for event in recovered["events"]]
    claim_workers = [event["worker_id"] for event in recovered["events"] if event["kind"] == "task.claimed"]
    if "demo.worker_killed" not in recovered_kinds or "task.lease_expired" not in recovered_kinds:
        raise AssertionError("The kill and expired-lease events were not both persisted.")
    if len(set(claim_workers)) < 2 or len(recovered["side_effects"]) != 4:
        raise AssertionError("The retry did not move to a different worker or preserve unique effects.")

    _, dlq, _ = call(base, token, "/api/demo/dlq", method="POST", body={})
    failed = wait_for(base, token, dlq["id"], lambda run: run["status"] == "failed")
    _, letters, _ = call(base, token, "/api/dead-letters")
    letter = next(item for item in letters if item["run_id"] == failed["id"] and not item["replayed_at"])
    _, replay, _ = call(base, token, f"/api/dead-letters/{letter['id']}/replay", method="POST", body={})
    replayed = wait_for(base, token, replay["run_id"], lambda run: run["status"] == "completed")
    _, status, _ = call(base, token, "/api/status")
    metrics_status, metrics_body, _ = call(base, token, "/metrics")
    if metrics_status != 200 or "relaycore_queue_depth" not in metrics_body:
        raise AssertionError("Prometheus metrics endpoint was not available.")

    report = {
        "classification": "MEASURED",
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "api": base,
        "duplicate_delivery": {"received": duplicate["received"], "logical_side_effects": len(duplicate_run["side_effects"]),
                               "workflow_id": duplicate_run["id"], "status": duplicate_run["status"]},
        "worker_kill_recovery": {"worker_id_killed": killed_worker, "worker_pid_killed": killed["pid"],
                                 "replacement_workers": sorted(set(claim_workers) - {killed_worker}),
                                 "lease_expired_event": "task.lease_expired" in recovered_kinds,
                                 "logical_side_effects": len(recovered["side_effects"]),
                                 "workflow_id": recovered["id"], "status": recovered["status"]},
        "dead_letter_replay": {"dead_letter_id": letter["id"], "workflow_id": replayed["id"],
                                "status": replayed["status"], "audit_events": [e["kind"] for e in replayed["events"]
                                                                               if e["kind"] in {"workflow.replayed", "workflow.completed"}]},
        "final_metrics": {"queue_depth": status["queue_depth"], "dead_letters": status["dlq_size"],
                          "side_effect_count": status["side_effect_count"], "latency_ms": status["latency_ms"]},
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "limitations": ["Charge effects are local PostgreSQL records, not real payments.",
                        "Failure/recovery runs in one local PostgreSQL deployment."],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
