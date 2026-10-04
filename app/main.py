from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from starlette.concurrency import run_in_threadpool

from app.coordinator import run as run_coordinator
from app.models import DemoFailureRequest, DuplicateEventRequest, WorkflowRequest, valid_idempotency_key
from app.settings import (
    DATABASE_URL,
    DEMO_MODE,
    LEASE_SECONDS,
    MAX_QUEUE_DEPTH,
    RATE_LIMIT_PER_MINUTE,
    WORKER_COUNT,
    api_keys,
)
from app.store import (
    IdempotencyConflict,
    QueueFull,
    RateLimited,
    create_workflow,
    dashboard,
    emit_event,
    ingest_business_event,
    migrate,
    run_summary,
)
from app.supervisor import WorkerSupervisor

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")
logger = logging.getLogger("relaycore.api")


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    role: str


def principal(authorization: str | None = Header(default=None)) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Provide a bearer API key.", headers={"WWW-Authenticate": "Bearer"})
    supplied = authorization[7:]
    for secret, identity in api_keys().items():
        if hmac.compare_digest(secret, supplied):
            return Principal(identity["tenant_id"], identity["role"])
    raise HTTPException(401, "API key is not valid.", headers={"WWW-Authenticate": "Bearer"})


def authorize(user: Principal, *roles: str) -> None:
    if user.role not in roles:
        raise HTTPException(403, "This API key does not have permission for that action.")


def pool_for(request: Request) -> ConnectionPool:
    return request.app.state.pool


def request_id(request: Request) -> str:
    return request.state.request_id


def rate_limit_error(exc: Exception) -> None:
    if isinstance(exc, QueueFull):
        raise HTTPException(429, f"Tenant queue is full ({MAX_QUEUE_DEPTH} active workflows). Retry later.")
    if isinstance(exc, RateLimited):
        raise HTTPException(429, f"Rate limit reached ({RATE_LIMIT_PER_MINUTE} writes per minute). Retry later.")
    if isinstance(exc, IdempotencyConflict):
        raise HTTPException(409, "This idempotency key was already used with a different payload.")
    if isinstance(exc, ValueError):
        raise HTTPException(422, str(exc))
    raise exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Reject missing/invalid secrets before opening a serving socket.
    api_keys()
    pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=16,
                          kwargs={"row_factory": dict_row, "application_name": "relaycore-api"},
                          check=ConnectionPool.check_connection, open=False)
    pool.open(wait=True)
    with pool.connection() as conn:
        encoding = conn.execute("SHOW server_encoding").fetchone()["server_encoding"]
    if encoding != "UTF8":
        pool.close()
        raise RuntimeError("RelayCore requires a UTF8 PostgreSQL database.")
    with pool.connection() as conn:
        migrate(conn)
    app.state.pool = pool
    stop = threading.Event()
    coordinator = threading.Thread(target=run_coordinator, args=(stop,), daemon=True, name="lease-coordinator")
    coordinator.start()
    supervisor = WorkerSupervisor(WORKER_COUNT)
    app.state.supervisor = supervisor
    if WORKER_COUNT:
        supervisor.start()
    logger.info('{"event":"api.started","workers":%d,"demo_mode":%s}', WORKER_COUNT, str(DEMO_MODE).lower())
    try:
        yield
    finally:
        supervisor.close()
        stop.set()
        coordinator.join(timeout=2)
        pool.close()
        logger.info('{"event":"api.stopped"}')


app = FastAPI(title="RelayCore", version="0.1.0", description="Durable, retry-safe workflow execution.", lifespan=lifespan)


@app.middleware("http")
async def correlate_request(request: Request, call_next):
    supplied = request.headers.get("x-request-id", "")
    rid = supplied[:80] if supplied and all(ch.isalnum() or ch in "._:-" for ch in supplied[:80]) else str(uuid.uuid4())
    request.state.request_id = rid
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    logger.info('{"event":"http.request","request_id":"%s","method":"%s","path":"%s","status":%d,"duration_ms":%.2f}',
                rid, request.method, request.url.path, response.status_code, (time.perf_counter() - started) * 1000)
    return response


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home() -> HTMLResponse:
    from pathlib import Path

    return HTMLResponse(Path(__file__).with_name("static").joinpath("index.html").read_text(encoding="utf-8"))


@app.get("/healthz", include_in_schema=False)
def health(request: Request) -> dict[str, str]:
    try:
        with pool_for(request).connection() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        raise HTTPException(503, "PostgreSQL is unavailable.") from exc
    return {"status": "ok"}


@app.get("/api/status")
def status(user: Principal = Depends(principal), pool: ConnectionPool = Depends(pool_for)) -> dict[str, Any]:
    with pool.connection() as conn:
        return dashboard(conn, user.tenant_id)


@app.get("/api/config")
def config(user: Principal = Depends(principal)) -> dict[str, Any]:
    return {"demo_mode": DEMO_MODE, "worker_count": WORKER_COUNT}


@app.post("/api/workflows", status_code=202)
def create(
    body: WorkflowRequest,
    request: Request,
    idempotency_key: str = Header(default_factory=lambda: str(uuid.uuid4()), alias="Idempotency-Key"),
    user: Principal = Depends(principal),
    pool: ConnectionPool = Depends(pool_for),
) -> dict[str, Any]:
    authorize(user, "admin", "operator")
    if not DEMO_MODE and any(step.action in {"fail_once", "fail_until_replay"} for step in body.steps):
        raise HTTPException(422, "Failure injection actions are available only in Demo Mode.")
    try:
        key = valid_idempotency_key(idempotency_key)
        with pool.connection() as conn:
            result = create_workflow(conn, user.tenant_id, body.title,
                                     [step.model_dump() for step in body.steps], key, request_id(request))
    except (QueueFull, RateLimited, IdempotencyConflict, ValueError) as exc:
        rate_limit_error(exc)
    return result


@app.get("/api/workflows")
def workflows(user: Principal = Depends(principal), pool: ConnectionPool = Depends(pool_for)) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        stats = dashboard(conn, user.tenant_id)
    return stats["workflows"]


@app.get("/api/workflows/{run_id}")
def workflow(run_id: str, user: Principal = Depends(principal), pool: ConnectionPool = Depends(pool_for)) -> dict[str, Any]:
    with pool.connection() as conn:
        result = run_summary(conn, user.tenant_id, run_id)
    if not result:
        raise HTTPException(404, "Workflow not found.")
    return result


@app.post("/api/workflows/{run_id}/cancel")
def cancel(run_id: str, request: Request, user: Principal = Depends(principal),
           pool: ConnectionPool = Depends(pool_for)) -> dict[str, str]:
    authorize(user, "admin", "operator")
    with pool.connection() as conn, conn.transaction():
        run = conn.execute("SELECT status FROM workflow_runs WHERE tenant_id=%s AND id=%s FOR UPDATE",
                           (user.tenant_id, run_id)).fetchone()
        if not run:
            raise HTTPException(404, "Workflow not found.")
        if run["status"] in {"completed", "failed", "cancelled"}:
            raise HTTPException(409, f"Workflow is already {run['status']}.")
        conn.execute("UPDATE workflow_runs SET status='cancelled',finished_at=clock_timestamp() WHERE id=%s", (run_id,))
        conn.execute("UPDATE tasks SET status='cancelled',lease_owner=NULL,lease_until=NULL,updated_at=clock_timestamp() WHERE run_id=%s", (run_id,))
        emit_event(conn, user.tenant_id, "workflow.cancelled", run_id=run_id, request_id=request_id(request),
                   data={"actor_role": user.role})
    return {"id": run_id, "status": "cancelled"}


@app.post("/api/demo/start", status_code=202)
def demo_start(request: Request, user: Principal = Depends(principal), pool: ConnectionPool = Depends(pool_for)) -> dict[str, Any]:
    authorize(user, "admin")
    require_demo()
    rid = request_id(request)
    steps = [
        {"name": "Reserve inventory", "action": "record", "payload": {"order": "demo-order-001"}},
        {"name": "Pause for worker kill demo", "action": "sleep", "payload": {"seconds": 4}},
        {"name": "Charge payment", "action": "charge", "payload": {"amount": 25, "currency": "USD"}},
        {"name": "Confirm shipment", "action": "record", "payload": {"order": "demo-order-001"}},
    ]
    try:
        with pool.connection() as conn:
            result = create_workflow(conn, user.tenant_id, "Demo order: reserve, charge, ship", steps,
                                     f"demo:{uuid.uuid4()}", rid)
    except (QueueFull, RateLimited, IdempotencyConflict) as exc:
        rate_limit_error(exc)
    return result


@app.post("/api/demo/duplicates")
def duplicate_demo(body: DuplicateEventRequest, request: Request,
                   user: Principal = Depends(principal), pool: ConnectionPool = Depends(pool_for)) -> dict[str, Any]:
    authorize(user, "admin")
    require_demo()
    request_key = request_id(request)
    payload = {"order": body.order, "amount": body.amount, "currency": body.currency}
    try:
        with pool.connection() as conn, conn.transaction():
            result = None
            for _ in range(10):
                result = ingest_business_event(conn, user.tenant_id, body.event_key, payload, request_key)
    except (QueueFull, RateLimited, IdempotencyConflict) as exc:
        rate_limit_error(exc)
    return {"deliveries_received": 10, "distinct_events": 1, "logical_workflows": 1,
            "logical_side_effects_expected": 3, **result}


@app.post("/api/demo/dlq", status_code=202)
def demo_dlq(body: DemoFailureRequest, request: Request,
             user: Principal = Depends(principal), pool: ConnectionPool = Depends(pool_for)) -> dict[str, Any]:
    authorize(user, "admin")
    require_demo()
    try:
        with pool.connection() as conn:
            return create_workflow(conn, user.tenant_id, body.title,
                                   [{"name": "Hold until an administrator replays", "action": "fail_until_replay", "payload": {}}],
                                   f"demo-dlq:{uuid.uuid4()}", request_id(request))
    except (QueueFull, RateLimited, IdempotencyConflict) as exc:
        rate_limit_error(exc)


def require_demo() -> None:
    if not DEMO_MODE:
        raise HTTPException(404, "This route is available only in Demo Mode.")


@app.get("/api/dead-letters")
def dead_letters(user: Principal = Depends(principal), pool: ConnectionPool = Depends(pool_for)) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        return conn.execute(
            """SELECT id,run_id,task_id,attempts,error,failed_at,replayed_at
               FROM dead_letters WHERE tenant_id=%s ORDER BY failed_at DESC LIMIT 100""",
            (user.tenant_id,),
        ).fetchall()


@app.post("/api/dead-letters/{dead_letter_id}/replay", status_code=202)
def replay(dead_letter_id: int, request: Request, user: Principal = Depends(principal),
           pool: ConnectionPool = Depends(pool_for)) -> dict[str, str]:
    authorize(user, "admin")
    with pool.connection() as conn, conn.transaction():
        item = conn.execute(
            "SELECT tenant_id,run_id,task_id,replayed_at FROM dead_letters WHERE id=%s AND tenant_id=%s FOR UPDATE",
            (dead_letter_id, user.tenant_id),
        ).fetchone()
        if not item:
            raise HTTPException(404, "Dead letter not found.")
        if item["replayed_at"]:
            raise HTTPException(409, "This dead letter has already been replayed.")
        conn.execute("UPDATE dead_letters SET replayed_at=clock_timestamp() WHERE id=%s", (dead_letter_id,))
        conn.execute("UPDATE tasks SET status='queued',attempts=0,last_error=NULL,available_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=%s",
                     (item["task_id"],))
        conn.execute("UPDATE workflow_runs SET status='queued',finished_at=NULL WHERE id=%s", (item["run_id"],))
        emit_event(conn, user.tenant_id, "workflow.replayed", run_id=item["run_id"], task_id=item["task_id"],
                   request_id=request_id(request), data={"dead_letter_id": dead_letter_id, "actor_role": user.role})
    return {"run_id": item["run_id"], "status": "queued"}


@app.post("/api/workers/{worker_id}/kill")
def kill_worker(worker_id: str, request: Request, user: Principal = Depends(principal),
                pool: ConnectionPool = Depends(pool_for)) -> dict[str, Any]:
    authorize(user, "admin")
    require_demo()
    with pool.connection() as conn, conn.transaction():
        task = conn.execute(
            """SELECT id,run_id FROM tasks WHERE tenant_id=%s AND status='running' AND lease_owner=%s
               AND lease_until>clock_timestamp() FOR UPDATE""", (user.tenant_id, worker_id)
        ).fetchone()
        if not task:
            raise HTTPException(409, "That worker has no active leased task to interrupt.")
        try:
            pid = request.app.state.supervisor.kill_worker(worker_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        emit_event(conn, user.tenant_id, "demo.worker_killed", run_id=task["run_id"], task_id=task["id"],
                   worker_id=worker_id, request_id=request_id(request),
                   data={"pid": pid, "failure_injection": True})
    return {"worker_id": worker_id, "pid": pid, "task_id": task["id"], "status": "killed; lease recovery pending"}


@app.post("/api/workers/{worker_id}/restart")
def restart_worker(worker_id: str, request: Request, user: Principal = Depends(principal),
                   pool: ConnectionPool = Depends(pool_for)) -> dict[str, Any]:
    authorize(user, "admin")
    require_demo()
    try:
        pid = request.app.state.supervisor.start_worker(worker_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    with pool.connection() as conn, conn.transaction():
        emit_event(conn, user.tenant_id, "demo.worker_restarted", worker_id=worker_id,
                   request_id=request_id(request), data={"pid": pid, "failure_injection": True})
    return {"worker_id": worker_id, "pid": pid, "status": "restarted"}


def _events_after(pool: ConnectionPool, tenant_id: str, sequence: int) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        return conn.execute(
            """SELECT sequence,run_id,task_id,worker_id,request_id,kind,data,created_at
               FROM events WHERE tenant_id=%s AND sequence>%s ORDER BY sequence LIMIT 100""",
            (tenant_id, sequence),
        ).fetchall()


@app.get("/api/events/stream")
async def event_stream(request: Request, after: int = 0, user: Principal = Depends(principal),
                       pool: ConnectionPool = Depends(pool_for)) -> StreamingResponse:
    async def stream():
        cursor = max(0, after)
        while not await request.is_disconnected():
            rows = await run_in_threadpool(_events_after, pool, user.tenant_id, cursor)
            for row in rows:
                cursor = row["sequence"]
                yield f"id: {cursor}\nevent: workflow\ndata: {json.dumps(row, default=str)}\n\n"
            if not rows:
                yield ": keepalive\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/metrics", include_in_schema=False)
def metrics(user: Principal = Depends(principal), pool: ConnectionPool = Depends(pool_for)) -> Response:
    with pool.connection() as conn:
        data = dashboard(conn, user.tenant_id)
    lines = [
        "# HELP relaycore_queue_depth Ready or leased tasks for the authenticated tenant.",
        "# TYPE relaycore_queue_depth gauge",
        f"relaycore_queue_depth {data['queue_depth']}",
        "# HELP relaycore_workflows_total Workflow runs by state.",
        "# TYPE relaycore_workflows_total gauge",
    ]
    for state in ("running", "completed", "failed"):
        lines.append(f'relaycore_workflows_total{{state="{state}"}} {data["counts"][state]}')
    lines.extend([
        "# HELP relaycore_side_effects_total Logical effects written for the authenticated tenant.",
        "# TYPE relaycore_side_effects_total counter",
        f"relaycore_side_effects_total {data['side_effect_count']}",
        "# HELP relaycore_dead_letters Current unreplayed dead letters.",
        "# TYPE relaycore_dead_letters gauge",
        f"relaycore_dead_letters {data['dlq_size']}",
    ])
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
