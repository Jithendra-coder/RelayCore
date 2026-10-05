from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from app.auth import Identity, Principal, SESSION_COOKIE, authenticated_identity, authorize, principal, verified_oidc_profile
from app.coordinator import run as run_coordinator
from app.models import (
    DemoFailureRequest,
    DuplicateEventRequest,
    CredentialCreateRequest,
    CredentialRotateRequest,
    WorkspaceCreateRequest,
    WorkspaceMemberRequest,
    WebhookCreateRequest,
    WorkflowRequest,
    valid_idempotency_key,
)
from app.settings import (
    DATABASE_URL,
    DEMO_MODE,
    AUTH_SESSION_SECONDS,
    MAX_WEBHOOK_BYTES,
    MAX_QUEUE_DEPTH,
    OIDC_SETTINGS,
    RATE_LIMIT_PER_MINUTE,
    WORKER_COUNT,
    WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS,
    api_keys,
    secret_encryption_key,
)
from app.store import (
    IdempotencyConflict,
    LastWorkspaceOwner,
    QueueFull,
    RateLimited,
    WorkflowDisabled,
    WebhookEndpointNotFound,
    WebhookSecretRotated,
    admit_rate_limit,
    add_workflow_version,
    create_auth_session,
    create_workflow,
    create_workflow_definition,
    create_workspace,
    create_webhook_endpoint,
    create_workspace_credential,
    dashboard,
    emit_event,
    get_workflow_definition,
    ingest_business_event,
    list_user_workspaces,
    list_incoming_events,
    list_webhook_endpoints,
    list_workspace_credentials,
    list_workflow_definitions,
    migrate,
    remove_workspace_member,
    revoke_webhook_endpoint,
    revoke_workspace_credential,
    revoke_auth_session,
    run_summary,
    set_workspace_member,
    rotate_webhook_secret,
    rotate_workspace_credential,
    trigger_workflow_definition,
    upsert_oidc_user,
    workspace_members,
    webhook_signing_info,
    persist_webhook_event,
    WorkspaceOwnerActionForbidden,
)
from app.secretbox import SecretStorageError, decrypt_secret
from app.supervisor import WorkerSupervisor

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")
logger = logging.getLogger("relaycore.api")


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
    if DEMO_MODE:
        api_keys()
    elif OIDC_SETTINGS is None:
        raise RuntimeError("Production requires OIDC configuration; static API keys are Demo Mode only.")
    else:
        secret_encryption_key(required=True)
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
    if OIDC_SETTINGS:
        from authlib.integrations.starlette_client import OAuth

        oauth = OAuth()
        oauth.register(
            "relaycore", client_id=OIDC_SETTINGS["RELAYCORE_OIDC_CLIENT_ID"],
            client_secret=OIDC_SETTINGS["RELAYCORE_OIDC_CLIENT_SECRET"],
            server_metadata_url=OIDC_SETTINGS["RELAYCORE_OIDC_DISCOVERY_URL"],
            client_kwargs={"scope": "openid email profile"},
        )
        app.state.oidc = oauth.create_client("relaycore")
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


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # Omit the submitted input and validator context: either can contain bearer credentials or webhook data.
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]})

if OIDC_SETTINGS:
    app.add_middleware(
        SessionMiddleware,
        secret_key=OIDC_SETTINGS["RELAYCORE_OIDC_STATE_SECRET"],
        session_cookie="relaycore_oidc_state",
        max_age=600,
        same_site="lax",
        https_only=True,
    )


def same_origin(origin: str | None, expected_url: str) -> bool:
    if not origin:
        return False
    actual, expected = urlsplit(origin), urlsplit(expected_url)
    return actual.scheme == expected.scheme and actual.netloc == expected.netloc


@app.middleware("http")
async def protect_cookie_mutations(request: Request, call_next):
    if (OIDC_SETTINGS and request.method not in {"GET", "HEAD", "OPTIONS"}
            and SESSION_COOKIE in request.cookies
            and not same_origin(request.headers.get("origin"), OIDC_SETTINGS["RELAYCORE_OIDC_REDIRECT_URI"])):
        return JSONResponse(status_code=403, content={"detail": "Cookie-authenticated changes require a same-origin request."})
    return await call_next(request)

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


@app.get("/auth/config")
def auth_config() -> dict[str, bool]:
    return {"oidc_enabled": OIDC_SETTINGS is not None, "demo_mode": DEMO_MODE}


@app.get("/auth/login", include_in_schema=False)
async def oidc_login(request: Request):
    if not OIDC_SETTINGS:
        raise HTTPException(404, "OpenID Connect is not configured.")
    return await request.app.state.oidc.authorize_redirect(
        request, OIDC_SETTINGS["RELAYCORE_OIDC_REDIRECT_URI"]
    )


@app.get("/auth/callback", include_in_schema=False)
async def oidc_callback(request: Request, pool: ConnectionPool = Depends(pool_for)):
    if not OIDC_SETTINGS:
        raise HTTPException(404, "OpenID Connect is not configured.")
    try:
        token = await request.app.state.oidc.authorize_access_token(request)
        claims = dict(token["userinfo"])
    except Exception as exc:
        request.session.clear()
        logger.warning('{"event":"auth.oidc_failed","request_id":"%s"}', request_id(request))
        raise HTTPException(401, "OpenID Connect sign-in could not be verified.") from exc

    try:
        issuer, subject, email, display_name = verified_oidc_profile(
            claims, OIDC_SETTINGS["RELAYCORE_OIDC_ISSUER"]
        )
    except ValueError as exc:
        request.session.clear()
        raise HTTPException(403, str(exc)) from exc

    with pool.connection() as conn, conn.transaction():
        user = upsert_oidc_user(conn, issuer, subject, email, display_name)
        if user["status"] != "active":
            raise HTTPException(403, "This account is disabled.")
        session_token = create_auth_session(conn, user["id"], request_id(request), AUTH_SESSION_SECONDS)
    request.session.clear()
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(SESSION_COOKIE, session_token, max_age=AUTH_SESSION_SECONDS,
                        httponly=True, secure=True, samesite="lax", path="/")
    return response


@app.post("/auth/logout", status_code=204)
def oidc_logout(
    request: Request,
    identity: Identity = Depends(authenticated_identity),
    pool: ConnectionPool = Depends(pool_for),
) -> Response:
    if not identity.session_token_hash:
        raise HTTPException(404, "Only signed-in sessions can be logged out here.")
    with pool.connection() as conn, conn.transaction():
        revoke_auth_session(conn, identity.session_token_hash, request_id(request))
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax", path="/")
    return response


@app.get("/api/me")
def me(identity: Identity = Depends(authenticated_identity),
       pool: ConnectionPool = Depends(pool_for)) -> dict[str, Any]:
    if identity.user_id is None:
        return {"authentication": "demo", "workspaces": [
            {"id": identity.demo_tenant, "name": identity.demo_tenant, "role": identity.demo_role}
        ]}
    with pool.connection() as conn:
        workspaces = list_user_workspaces(conn, identity.user_id)
    return {"id": identity.user_id, "email": identity.email, "name": identity.display_name,
            "workspaces": workspaces}


@app.get("/api/workspaces")
def workspaces(identity: Identity = Depends(authenticated_identity),
               pool: ConnectionPool = Depends(pool_for)) -> list[dict[str, Any]]:
    if identity.user_id is None:
        return [{"id": identity.demo_tenant, "name": identity.demo_tenant, "role": identity.demo_role}]
    with pool.connection() as conn:
        return list_user_workspaces(conn, identity.user_id)


@app.post("/api/workspaces", status_code=201)
def new_workspace(body: WorkspaceCreateRequest, request: Request,
                  identity: Identity = Depends(authenticated_identity),
                  pool: ConnectionPool = Depends(pool_for)) -> dict[str, Any]:
    if identity.user_id is None:
        raise HTTPException(403, "Create workspaces with a signed-in user account.")
    with pool.connection() as conn, conn.transaction():
        return create_workspace(conn, identity.user_id, body.name, request_id(request))


@app.get("/api/workspaces/{workspace_id}/members")
def members(workspace_id: str, user: Principal = Depends(principal),
            pool: ConnectionPool = Depends(pool_for)) -> list[dict[str, Any]]:
    if user.tenant_id != workspace_id:
        raise HTTPException(404, "Workspace not found.")
    with pool.connection() as conn:
        return workspace_members(conn, workspace_id)


@app.put("/api/workspaces/{workspace_id}/members", status_code=200)
def set_member(workspace_id: str, body: WorkspaceMemberRequest, request: Request,
               user: Principal = Depends(principal), pool: ConnectionPool = Depends(pool_for)) -> dict[str, str]:
    if user.tenant_id != workspace_id:
        raise HTTPException(404, "Workspace not found.")
    authorize(user, "admin")
    if user.user_id is None:
        raise HTTPException(403, "Membership management requires a signed-in workspace owner.")
    if body.role in {"OWNER", "ADMIN"} and user.workspace_role != "OWNER":
        raise HTTPException(403, "Only a workspace owner can grant owner or admin roles.")
    try:
        with pool.connection() as conn, conn.transaction():
            found = set_workspace_member(conn, workspace_id, user.user_id or "", str(body.user_id),
                                         body.role, request_id(request))
    except LastWorkspaceOwner as exc:
        raise HTTPException(409, "A workspace must keep at least one owner.") from exc
    except WorkspaceOwnerActionForbidden as exc:
        raise HTTPException(403, "Only a workspace owner can change another owner's membership.") from exc
    if not found:
        raise HTTPException(404, "User not found.")
    return {"workspace_id": workspace_id, "user_id": str(body.user_id), "role": body.role}


@app.delete("/api/workspaces/{workspace_id}/members/{member_id}", status_code=204)
def delete_member(workspace_id: str, member_id: str, request: Request,
                  user: Principal = Depends(principal), pool: ConnectionPool = Depends(pool_for)) -> Response:
    if user.tenant_id != workspace_id:
        raise HTTPException(404, "Workspace not found.")
    authorize(user, "admin")
    if user.user_id is None:
        raise HTTPException(403, "Membership management requires a signed-in workspace owner.")
    try:
        with pool.connection() as conn, conn.transaction():
            found = remove_workspace_member(conn, workspace_id, user.user_id or "", member_id,
                                            request_id(request))
    except LastWorkspaceOwner as exc:
        raise HTTPException(409, "A workspace must keep at least one owner.") from exc
    except WorkspaceOwnerActionForbidden as exc:
        raise HTTPException(403, "Only a workspace owner can remove another owner.") from exc
    if not found:
        raise HTTPException(404, "Workspace member not found.")
    return Response(status_code=204)


def webhook_encryption_key() -> bytes:
    try:
        key = secret_encryption_key()
    except RuntimeError as exc:
        raise HTTPException(503, "Webhook secret storage is unavailable.") from exc
    if key is None:
        raise HTTPException(503, "Configure encrypted secret storage before creating webhook endpoints.")
    return key


def credential_encryption_key() -> bytes:
    try:
        key = secret_encryption_key()
    except RuntimeError as exc:
        raise HTTPException(503, "Credential secret storage is unavailable.") from exc
    if key is None:
        raise HTTPException(503, "Configure encrypted secret storage before creating credentials.")
    return key


def credential_secret_value(secret: Any) -> str:
    value = secret.get_secret_value()
    if not 16 <= len(value) <= 4096:
        raise HTTPException(422, "Credential secret must be between 16 and 4096 characters.")
    return value


@app.get("/api/workspaces/{workspace_id}/credentials")
def credentials(workspace_id: str, user: Principal = Depends(principal),
                pool: ConnectionPool = Depends(pool_for)) -> list[dict[str, Any]]:
    if user.tenant_id != workspace_id:
        raise HTTPException(404, "Workspace not found.")
    authorize(user, "admin")
    with pool.connection() as conn:
        return list_workspace_credentials(conn, workspace_id)


@app.post("/api/workspaces/{workspace_id}/credentials", status_code=201)
def new_credential(workspace_id: str, body: CredentialCreateRequest, request: Request,
                   idempotency_key: str = Header(default_factory=lambda: str(uuid.uuid4()), alias="Idempotency-Key"),
                   user: Principal = Depends(principal), pool: ConnectionPool = Depends(pool_for)) -> dict[str, Any]:
    if user.tenant_id != workspace_id:
        raise HTTPException(404, "Workspace not found.")
    authorize(user, "admin")
    if user.user_id is None:
        raise HTTPException(403, "Credentials require a signed-in workspace administrator.")
    try:
        key = valid_idempotency_key(idempotency_key)
        with pool.connection() as conn, conn.transaction():
            return create_workspace_credential(conn, workspace_id, user.user_id, body.provider, body.name,
                                               credential_secret_value(body.secret), key, request_id(request),
                                               credential_encryption_key())
    except IdempotencyConflict as exc:
        raise HTTPException(409, "This idempotency key was already used for a different or revoked credential.") from exc
    except SecretStorageError as exc:
        raise HTTPException(503, "Credential secret storage is unavailable.") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/workspaces/{workspace_id}/credentials/{credential_id}/rotate")
def rotate_credential(workspace_id: str, credential_id: str, body: CredentialRotateRequest, request: Request,
                      idempotency_key: str = Header(default_factory=lambda: str(uuid.uuid4()), alias="Idempotency-Key"),
                      user: Principal = Depends(principal), pool: ConnectionPool = Depends(pool_for)) -> dict[str, Any]:
    if user.tenant_id != workspace_id:
        raise HTTPException(404, "Workspace not found.")
    authorize(user, "admin")
    if user.user_id is None:
        raise HTTPException(403, "Credential rotation requires a signed-in workspace administrator.")
    try:
        key = valid_idempotency_key(idempotency_key)
        with pool.connection() as conn, conn.transaction():
            result = rotate_workspace_credential(conn, workspace_id, credential_id, user.user_id,
                                                 credential_secret_value(body.secret), key, request_id(request),
                                                 credential_encryption_key())
    except IdempotencyConflict as exc:
        raise HTTPException(409, "This idempotency key was already used with a different credential secret.") from exc
    except SecretStorageError as exc:
        raise HTTPException(503, "Credential secret storage is unavailable.") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not result:
        raise HTTPException(404, "Credential not found.")
    return result


@app.delete("/api/workspaces/{workspace_id}/credentials/{credential_id}", status_code=204)
def delete_credential(workspace_id: str, credential_id: str, request: Request,
                      user: Principal = Depends(principal), pool: ConnectionPool = Depends(pool_for)) -> Response:
    if user.tenant_id != workspace_id:
        raise HTTPException(404, "Workspace not found.")
    authorize(user, "admin")
    if user.user_id is None:
        raise HTTPException(403, "Credential revocation requires a signed-in workspace administrator.")
    with pool.connection() as conn, conn.transaction():
        found = revoke_workspace_credential(conn, workspace_id, credential_id, user.user_id, request_id(request))
    if not found:
        raise HTTPException(404, "Credential not found.")
    return Response(status_code=204)


@app.get("/api/workspaces/{workspace_id}/webhooks")
def webhooks(workspace_id: str, user: Principal = Depends(principal),
             pool: ConnectionPool = Depends(pool_for)) -> list[dict[str, Any]]:
    if user.tenant_id != workspace_id:
        raise HTTPException(404, "Workspace not found.")
    authorize(user, "admin")
    with pool.connection() as conn:
        return list_webhook_endpoints(conn, workspace_id)


@app.post("/api/workspaces/{workspace_id}/webhooks", status_code=201)
def new_webhook(workspace_id: str, body: WebhookCreateRequest, request: Request,
                idempotency_key: str = Header(default_factory=lambda: str(uuid.uuid4()), alias="Idempotency-Key"),
                user: Principal = Depends(principal), pool: ConnectionPool = Depends(pool_for)) -> dict[str, Any]:
    if user.tenant_id != workspace_id:
        raise HTTPException(404, "Workspace not found.")
    authorize(user, "admin")
    if user.user_id is None:
        raise HTTPException(403, "Webhook endpoints require a signed-in workspace administrator.")
    try:
        key = valid_idempotency_key(idempotency_key)
        with pool.connection() as conn, conn.transaction():
            return create_webhook_endpoint(conn, workspace_id, user.user_id, body.name, key,
                                           request_id(request), webhook_encryption_key())
    except IdempotencyConflict as exc:
        raise HTTPException(409, "This idempotency key was already used for a different or revoked webhook.") from exc
    except SecretStorageError as exc:
        raise HTTPException(503, "Webhook secret storage is unavailable.") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/workspaces/{workspace_id}/webhooks/{endpoint_id}/rotate", status_code=200)
def rotate_webhook(workspace_id: str, endpoint_id: str, request: Request,
                   idempotency_key: str = Header(default_factory=lambda: str(uuid.uuid4()), alias="Idempotency-Key"),
                   user: Principal = Depends(principal), pool: ConnectionPool = Depends(pool_for)) -> dict[str, Any]:
    if user.tenant_id != workspace_id:
        raise HTTPException(404, "Workspace not found.")
    authorize(user, "admin")
    if user.user_id is None:
        raise HTTPException(403, "Webhook secret rotation requires a signed-in workspace administrator.")
    try:
        key = valid_idempotency_key(idempotency_key)
        with pool.connection() as conn, conn.transaction():
            result = rotate_webhook_secret(conn, workspace_id, endpoint_id, user.user_id, key,
                                           request_id(request), webhook_encryption_key())
    except IdempotencyConflict as exc:
        raise HTTPException(409, "This idempotency key was already used by a superseded webhook secret.") from exc
    except SecretStorageError as exc:
        raise HTTPException(503, "Webhook secret storage is unavailable.") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not result:
        raise HTTPException(404, "Webhook endpoint not found.")
    return result


@app.delete("/api/workspaces/{workspace_id}/webhooks/{endpoint_id}", status_code=204)
def delete_webhook(workspace_id: str, endpoint_id: str, request: Request,
                   user: Principal = Depends(principal), pool: ConnectionPool = Depends(pool_for)) -> Response:
    if user.tenant_id != workspace_id:
        raise HTTPException(404, "Workspace not found.")
    authorize(user, "admin")
    if user.user_id is None:
        raise HTTPException(403, "Webhook revocation requires a signed-in workspace administrator.")
    with pool.connection() as conn, conn.transaction():
        found = revoke_webhook_endpoint(conn, workspace_id, endpoint_id, user.user_id, request_id(request))
    if not found:
        raise HTTPException(404, "Webhook endpoint not found.")
    return Response(status_code=204)


async def read_webhook_payload(request: Request) -> tuple[bytes, dict[str, Any]]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(415, "Webhook content type must be application/json.")
    declared_length = request.headers.get("content-length")
    if declared_length:
        if not declared_length.isdigit():
            raise HTTPException(400, "Content-Length is invalid.")
        if int(declared_length) > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "Webhook payload exceeds the 256 KiB limit.")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "Webhook payload exceeds the 256 KiB limit.")
        chunks.append(chunk)
    raw_body = b"".join(chunks)
    def reject_json_constant(_: str) -> None:
        raise ValueError("Non-standard JSON number.")
    try:
        payload = json.loads(raw_body, parse_constant=reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise HTTPException(400, "Webhook body must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(422, "Webhook JSON must be an object.")
    return raw_body, payload


@app.post("/hooks/{endpoint_id}", status_code=202)
async def receive_webhook(
    endpoint_id: str,
    request: Request,
    event_key: str | None = Header(default=None, alias="X-RelayCore-Event-ID"),
    timestamp: str | None = Header(default=None, alias="X-RelayCore-Timestamp"),
    signature: str | None = Header(default=None, alias="X-RelayCore-Signature"),
    pool: ConnectionPool = Depends(pool_for),
) -> dict[str, Any]:
    if not event_key or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", event_key):
        raise HTTPException(400, "X-RelayCore-Event-ID is required and must be a valid event key.")
    if not timestamp or not re.fullmatch(r"\d{1,12}", timestamp):
        raise HTTPException(401, "Webhook timestamp is missing or invalid.")
    if abs(time.time() - int(timestamp)) > WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS:
        raise HTTPException(401, "Webhook timestamp is outside the five-minute replay window.")
    if not signature or not re.fullmatch(r"sha256=[0-9a-fA-F]{64}", signature):
        raise HTTPException(401, "Webhook signature is missing or invalid.")

    with pool.connection() as conn:
        endpoint = webhook_signing_info(conn, endpoint_id)
    if not endpoint:
        raise HTTPException(404, "Webhook endpoint not found.")
    encryption_key = webhook_encryption_key()
    try:
        secret = decrypt_secret(endpoint["encrypted_secret"], encryption_key)
    except SecretStorageError as exc:
        logger.error('{"event":"webhook.secret_unavailable","request_id":"%s"}', request_id(request))
        raise HTTPException(503, "Webhook secret storage is unavailable.") from exc
    try:
        with pool.connection() as conn, conn.transaction():
            admit_rate_limit(conn, endpoint["workspace_id"])
    except RateLimited as exc:
        raise HTTPException(429, "Workspace webhook rate limit reached.") from exc

    raw_body, payload = await read_webhook_payload(request)
    signed_content = timestamp.encode() + b"\n" + event_key.encode() + b"\n" + raw_body
    expected = hmac.new(secret.encode(), signed_content, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature[7:].lower(), expected):
        raise HTTPException(401, "Webhook signature did not match the request body.")
    try:
        with pool.connection() as conn, conn.transaction():
            result = persist_webhook_event(conn, endpoint["workspace_id"], endpoint_id,
                                           endpoint["version"], event_key, request_id(request), raw_body, payload,
                                           allow_workflow_triggers=DEMO_MODE)
    except IdempotencyConflict as exc:
        raise HTTPException(409, "This event ID was already used with a different payload.") from exc
    except WebhookSecretRotated as exc:
        raise HTTPException(401, "Webhook secret changed during verification; sign with the active secret.") from exc
    except QueueFull as exc:
        raise HTTPException(429, "Workspace workflow queue is full; retry this webhook later.") from exc
    except RateLimited as exc:
        raise HTTPException(429, "Workspace webhook rate limit reached.") from exc
    return {"event_id": result["id"], "event_key": event_key, "accepted": True,
            "duplicate": result["duplicate"], "triggered_runs": result.get("triggered_runs", [])}


@app.get("/api/status")
def status(user: Principal = Depends(principal), pool: ConnectionPool = Depends(pool_for)) -> dict[str, Any]:
    with pool.connection() as conn:
        return dashboard(conn, user.tenant_id, include_workers=DEMO_MODE)


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
    if not DEMO_MODE:
        raise HTTPException(501, "Production workflow actions are not implemented yet. Enable Demo Mode for local simulations.")
    try:
        key = valid_idempotency_key(idempotency_key)
        with pool.connection() as conn:
            result = create_workflow(conn, user.tenant_id, body.title,
                                     [step.model_dump() for step in body.steps], key, request_id(request))
    except (QueueFull, RateLimited, IdempotencyConflict, ValueError) as exc:
        rate_limit_error(exc)
    return result


@app.get("/api/workflow-definitions")
def workflow_definitions(user: Principal = Depends(principal),
                         pool: ConnectionPool = Depends(pool_for)) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        return list_workflow_definitions(conn, user.tenant_id)


@app.post("/api/workflow-definitions", status_code=201)
def create_definition(
    body: WorkflowRequest,
    request: Request,
    idempotency_key: str = Header(default_factory=lambda: str(uuid.uuid4()), alias="Idempotency-Key"),
    user: Principal = Depends(principal),
    pool: ConnectionPool = Depends(pool_for),
) -> dict[str, Any]:
    authorize(user, "admin", "operator")
    if not DEMO_MODE:
        raise HTTPException(501, "Production workflow actions are not implemented yet.")
    try:
        key = valid_idempotency_key(idempotency_key)
        with pool.connection() as conn:
            return create_workflow_definition(
                conn, user.tenant_id, body.title, [step.model_dump() for step in body.steps],
                key, user.credential_fingerprint, request_id(request), user.user_id,
                trigger=body.trigger.model_dump(mode="json") if body.trigger else None,
            )
    except WebhookEndpointNotFound as exc:
        raise HTTPException(404, "Webhook trigger endpoint not found in this workspace.") from exc
    except (IdempotencyConflict, ValueError) as exc:
        rate_limit_error(exc)


@app.get("/api/workflow-definitions/{workflow_id}")
def workflow_definition(workflow_id: str, user: Principal = Depends(principal),
                        pool: ConnectionPool = Depends(pool_for)) -> dict[str, Any]:
    with pool.connection() as conn:
        result = get_workflow_definition(conn, user.tenant_id, workflow_id)
    if not result:
        raise HTTPException(404, "Workflow definition not found.")
    return result


@app.post("/api/workflow-definitions/{workflow_id}/versions", status_code=201)
def publish_workflow_version(
    workflow_id: str,
    body: WorkflowRequest,
    request: Request,
    idempotency_key: str = Header(default_factory=lambda: str(uuid.uuid4()), alias="Idempotency-Key"),
    user: Principal = Depends(principal),
    pool: ConnectionPool = Depends(pool_for),
) -> dict[str, Any]:
    authorize(user, "admin", "operator")
    if not DEMO_MODE:
        raise HTTPException(501, "Production workflow actions are not implemented yet.")
    try:
        key = valid_idempotency_key(idempotency_key)
        with pool.connection() as conn:
            result = add_workflow_version(
                conn, user.tenant_id, workflow_id, body.title, [step.model_dump() for step in body.steps],
                key, user.credential_fingerprint, request_id(request), user.user_id,
                trigger=body.trigger.model_dump(mode="json") if body.trigger else None,
            )
    except WebhookEndpointNotFound as exc:
        raise HTTPException(404, "Webhook trigger endpoint not found in this workspace.") from exc
    except WorkflowDisabled as exc:
        raise HTTPException(409, "Workflow is disabled.") from exc
    except (IdempotencyConflict, ValueError) as exc:
        rate_limit_error(exc)
    if result is None:
        raise HTTPException(404, "Workflow definition not found.")
    return result


@app.post("/api/workflow-definitions/{workflow_id}/runs", status_code=202)
def run_workflow_definition(
    workflow_id: str,
    request: Request,
    idempotency_key: str = Header(default_factory=lambda: str(uuid.uuid4()), alias="Idempotency-Key"),
    user: Principal = Depends(principal),
    pool: ConnectionPool = Depends(pool_for),
) -> dict[str, Any]:
    authorize(user, "admin", "operator")
    if not DEMO_MODE:
        raise HTTPException(501, "Production workflow actions are not implemented yet.")
    try:
        key = valid_idempotency_key(idempotency_key)
        with pool.connection() as conn:
            result = trigger_workflow_definition(conn, user.tenant_id, workflow_id, key, request_id(request))
    except WorkflowDisabled as exc:
        raise HTTPException(409, "Workflow is disabled.") from exc
    except (QueueFull, RateLimited, IdempotencyConflict, ValueError) as exc:
        rate_limit_error(exc)
    if result is None:
        raise HTTPException(404, "Workflow definition not found.")
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


def event_cursor(after: int | None, last_event_id: str | None) -> int:
    cursor = max(0, after or 0)
    if last_event_id is not None:
        try:
            last_sequence = int(last_event_id)
        except ValueError as exc:
            raise HTTPException(400, "Last-Event-ID must be a non-negative integer.") from exc
        if last_sequence < 0:
            raise HTTPException(400, "Last-Event-ID must be a non-negative integer.")
        cursor = max(cursor, last_sequence)
    if cursor > 9_223_372_036_854_775_807:
        raise HTTPException(400, "Event cursor is out of range.")
    return cursor


def decode_event_page_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if cursor is None:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True))
        if not isinstance(value, list) or len(value) != 2 or not all(isinstance(part, str) for part in value):
            raise ValueError
        received_at = datetime.fromisoformat(value[0])
        event_id = str(uuid.UUID(value[1]))
        if received_at.tzinfo is None:
            raise ValueError
        return received_at, event_id
    except (ValueError, TypeError, UnicodeEncodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise HTTPException(400, "Event cursor is invalid.") from exc


@app.get("/api/events")
def events(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=256),
    user: Principal = Depends(principal),
    pool: ConnectionPool = Depends(pool_for),
) -> dict[str, Any]:
    before = decode_event_page_cursor(cursor)
    with pool.connection() as conn:
        rows = list_incoming_events(conn, user.tenant_id, limit, before)
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = None
    if has_more:
        last = items[-1]
        encoded = json.dumps([last["received_at"].isoformat(), last["id"]], separators=(",", ":")).encode()
        next_cursor = base64.b64encode(encoded, altchars=b"-_").decode().rstrip("=")
    return {"items": items, "next_cursor": next_cursor}


@app.get("/api/events/stream")
async def event_stream(request: Request, after: int | None = None,
                       last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
                       user: Principal = Depends(principal),
                       pool: ConnectionPool = Depends(pool_for)) -> StreamingResponse:
    cursor = event_cursor(after, last_event_id)

    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            rows = await run_in_threadpool(_events_after, pool, user.tenant_id, cursor)
            for row in rows:
                cursor = row["sequence"]
                yield f"id: {cursor}\nevent: workflow\ndata: {json.dumps(row, default=str)}\n\n"
            if not rows:
                yield ": keepalive\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


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
