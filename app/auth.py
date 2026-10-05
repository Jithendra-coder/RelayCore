from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request

from app.settings import DEMO_MODE, api_keys
from app.store import auth_session, list_user_workspaces, workspace_membership

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
        if not DEMO_MODE or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer API keys are available only in Demo Mode.",
                                headers={"WWW-Authenticate": "Bearer"})
        supplied = authorization[7:]
        for secret, identity in api_keys().items():
            if hmac.compare_digest(secret, supplied):
                fingerprint = hashlib.sha256(secret.encode()).hexdigest()
                return Identity(None, None, None, fingerprint, demo_tenant=identity["tenant_id"],
                                demo_role=identity["role"])
        raise HTTPException(401, "API key is not valid.", headers={"WWW-Authenticate": "Bearer"})

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
    if identity.demo_tenant is not None:
        return Principal(identity.demo_tenant, identity.demo_role or "viewer",
                         identity.credential_fingerprint)

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
