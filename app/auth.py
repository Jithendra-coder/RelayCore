from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request
from psycopg import Connection

from app.settings import DEMO_MODE, api_keys
from app.store import auth_session, list_user_workspaces, workspace_api_token, workspace_membership

SESSION_COOKIE = "relaycore_session"


def verified_oidc_profile(claims: dict, expected_issuer: str) -> tuple[str, str, str, str]:
    issuer, subject = claims.get("iss"), claims.get("sub")
    email, verified = claims.get("email"), claims.get("email_verified")
    if (issuer != expected_issuer or not isinstance(subject, str) or not subject or len(subject) > 255
            or not isinstance(email, str) or len(email) > 320 or "@" not in email
            or any(ch.isspace() for ch in email) or verified is not True):
        raise ValueError("Use an OIDC account with a verified email address.")
    return issuer, subject, email, str(claims.get("name") or email)


@dataclass(frozen=True)
class Identity:
    user_id: str | None
    email: str | None
    display_name: str | None
    credential_fingerprint: str
    session_token_hash: str | None = None
    demo_tenant: str | None = None
    demo_role: str | None = None
    workspace_id: str | None = None


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    role: str
    credential_fingerprint: str
    user_id: str | None = None
    workspace_role: str | None = None
    session_token_hash: str | None = None
    email: str | None = None


def authenticated_identity(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Identity:
    if authorization is not None:
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "Use a bearer token.", headers={"WWW-Authenticate": "Bearer"})
        supplied = authorization[7:]
        if DEMO_MODE:
            for secret, demo_identity in api_keys().items():
                if hmac.compare_digest(secret, supplied):
                    fingerprint = hashlib.sha256(secret.encode()).hexdigest()
                    return Identity(None, None, None, fingerprint, demo_tenant=demo_identity["tenant_id"],
                                    demo_role=demo_identity["role"])
        elif 32 <= len(supplied) <= 256:
            with request.app.state.pool.connection() as conn:
                token = workspace_api_token(conn, hashlib.sha256(supplied.encode()).hexdigest())
            if token:
                return Identity(token["user_id"], token["email"], token["display_name"],
                                token["token_hash"], workspace_id=token["workspace_id"])
        raise HTTPException(401, "Bearer token is not valid.", headers={"WWW-Authenticate": "Bearer"})

    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with request.app.state.pool.connection() as conn:
            session = auth_session(conn, token)
        if session:
            return Identity(session["user_id"], session["email"], session["display_name"],
                            session["token_hash"], session_token_hash=session["token_hash"])
    raise HTTPException(401, "Sign in to continue.", headers={"WWW-Authenticate": "Bearer"})


def principal(
    request: Request,
    identity: Identity = Depends(authenticated_identity),
    selected_workspace_id: str | None = Header(default=None, alias="X-Workspace-ID"),
) -> Principal:
    return principal_for_identity(request, identity, selected_workspace_id)


def principal_for_identity(
    request: Request, identity: Identity, selected_workspace_id: str | None = None,
) -> Principal:
    if identity.demo_tenant is not None:
        return Principal(identity.demo_tenant, identity.demo_role or "viewer",
                         identity.credential_fingerprint)

    if identity.workspace_id:
        if selected_workspace_id and selected_workspace_id != identity.workspace_id:
            raise HTTPException(404, "Workspace not found.")
        selected_workspace_id = identity.workspace_id

    with request.app.state.pool.connection() as conn:
        memberships = list_user_workspaces(conn, identity.user_id or "")
        if not memberships:
            raise HTTPException(403, "Create or join a workspace before using RelayCore.")
        if selected_workspace_id is None:
            if len(memberships) > 1:
                raise HTTPException(400, "Select a workspace with the X-Workspace-ID header.")
            selected_workspace_id = memberships[0]["id"]
        membership = workspace_membership(conn, selected_workspace_id, identity.user_id or "")
    if not membership:
        raise HTTPException(404, "Workspace not found.")

    role = membership["role"]
    effective_role = {"OWNER": "admin", "ADMIN": "admin", "DEVELOPER": "operator", "VIEWER": "viewer"}[role]
    return Principal(membership["workspace_id"], effective_role, identity.credential_fingerprint,
                     user_id=identity.user_id, workspace_role=role,
                     session_token_hash=identity.session_token_hash, email=identity.email)


def authorize(user: Principal, *roles: str) -> None:
    if user.role not in roles:
        raise HTTPException(403, "This identity does not have permission for that action.")


def principal_is_current(conn: Connection, user: Principal) -> bool:
    if user.user_id is None:
        return True
    if not workspace_membership(conn, user.tenant_id, user.user_id):
        return False
    if user.session_token_hash:
        return conn.execute(
            """SELECT 1 FROM auth_sessions s JOIN users u ON u.id=s.user_id
               WHERE s.token_hash=%s AND s.user_id=%s AND s.revoked_at IS NULL
                 AND s.expires_at>clock_timestamp() AND u.status='active'""",
            (user.session_token_hash, user.user_id),
        ).fetchone() is not None
    token = workspace_api_token(conn, user.credential_fingerprint)
    return bool(token and token["workspace_id"] == user.tenant_id and token["user_id"] == user.user_id)
