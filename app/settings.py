from __future__ import annotations

import json
import math
import os
from typing import Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres@127.0.0.1:55432/postgres")
DEMO_MODE = os.environ.get("RELAYCORE_DEMO_MODE", "0").lower() in {"1", "true", "yes"}
WORKER_COUNT = int(os.environ.get("RELAYCORE_WORKERS", "2"))
MAX_ATTEMPTS = int(os.environ.get("RELAYCORE_MAX_ATTEMPTS", "3"))
MAX_QUEUE_DEPTH = int(os.environ.get("RELAYCORE_QUEUE_LIMIT", "1000"))
MAX_SCHEDULES_PER_TENANT = int(os.environ.get("RELAYCORE_SCHEDULE_LIMIT", "1000"))
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RELAYCORE_RATE_LIMIT_PER_MINUTE", "600"))
MAX_WEBHOOK_BYTES = 256 * 1024
WEBHOOK_PAYLOAD_RETENTION_DAYS = int(os.environ.get("RELAYCORE_WEBHOOK_PAYLOAD_RETENTION_DAYS", "30"))
WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 300
LEASE_SECONDS = float(os.environ.get("RELAYCORE_LEASE_SECONDS", "4"))
AUTH_SESSION_SECONDS = int(os.environ.get("RELAYCORE_AUTH_SESSION_SECONDS", "43200"))
if WORKER_COUNT < 0:
    raise RuntimeError("RELAYCORE_WORKERS must be zero or greater.")
if not 1 <= MAX_ATTEMPTS <= 10:
    raise RuntimeError("RELAYCORE_MAX_ATTEMPTS must be between 1 and 10.")
if MAX_QUEUE_DEPTH < 1:
    raise RuntimeError("RELAYCORE_QUEUE_LIMIT must be at least 1.")
if MAX_SCHEDULES_PER_TENANT < 1:
    raise RuntimeError("RELAYCORE_SCHEDULE_LIMIT must be at least 1.")
if RATE_LIMIT_PER_MINUTE < 1:
    raise RuntimeError("RELAYCORE_RATE_LIMIT_PER_MINUTE must be at least 1.")
if not 1 <= WEBHOOK_PAYLOAD_RETENTION_DAYS <= 3650:
    raise RuntimeError("RELAYCORE_WEBHOOK_PAYLOAD_RETENTION_DAYS must be between 1 and 3650.")
if not math.isfinite(LEASE_SECONDS) or LEASE_SECONDS < 0.2:
    raise RuntimeError("RELAYCORE_LEASE_SECONDS must be finite and at least 0.2 seconds.")
if not 300 <= AUTH_SESSION_SECONDS <= 604800:
    raise RuntimeError("RELAYCORE_AUTH_SESSION_SECONDS must be between 300 and 604800.")


def oidc_settings() -> dict[str, str] | None:
    names = ("RELAYCORE_OIDC_ISSUER", "RELAYCORE_OIDC_CLIENT_ID",
             "RELAYCORE_OIDC_CLIENT_SECRET", "RELAYCORE_OIDC_REDIRECT_URI", "RELAYCORE_OIDC_STATE_SECRET")
    values = {name: os.environ.get(name, "").strip() for name in names}
    configured = [bool(value) for value in values.values()]
    if not any(configured):
        return None
    if not all(configured):
        missing = ", ".join(name for name, value in values.items() if not value)
        raise RuntimeError(f"Incomplete OIDC configuration; missing {missing}.")
    for name in ("RELAYCORE_OIDC_ISSUER", "RELAYCORE_OIDC_REDIRECT_URI"):
        parsed = urlsplit(values[name])
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise RuntimeError(f"{name} must be an HTTPS URL without embedded credentials.")
    if len(values["RELAYCORE_OIDC_STATE_SECRET"]) < 32:
        raise RuntimeError("RELAYCORE_OIDC_STATE_SECRET must contain at least 32 characters.")
    values["RELAYCORE_OIDC_DISCOVERY_URL"] = os.environ.get(
        "RELAYCORE_OIDC_DISCOVERY_URL",
        f"{values['RELAYCORE_OIDC_ISSUER'].rstrip('/')}/.well-known/openid-configuration",
    ).strip()
    discovery = urlsplit(values["RELAYCORE_OIDC_DISCOVERY_URL"])
    if discovery.scheme != "https" or not discovery.netloc or discovery.username or discovery.password:
        raise RuntimeError("RELAYCORE_OIDC_DISCOVERY_URL must be an HTTPS URL without embedded credentials.")
    return values


def api_keys() -> dict[str, dict[str, str]]:
    raw = os.environ.get("RELAYCORE_API_KEYS")
    if not raw and DEMO_MODE:
        return {"demo-key-change-me-32": {"tenant_id": "demo", "role": "admin"}}
    try:
        keys: Any = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("RELAYCORE_API_KEYS must be a JSON object.") from exc
    if not DEMO_MODE:
        raise RuntimeError("Static API keys are permitted only in Demo Mode; configure OIDC for production.")
    if not isinstance(keys, dict) or not keys:
        raise RuntimeError("Set RELAYCORE_API_KEYS before starting RelayCore.")
    for secret, identity in keys.items():
        if len(secret) < 16 or not isinstance(identity, dict) or not identity.get("tenant_id"):
            raise RuntimeError("Each API key must be at least 16 characters with a tenant_id and role.")
        if identity.get("role") not in {"admin", "operator", "viewer"}:
            raise RuntimeError("API key role must be admin, operator, or viewer.")
    return keys


def secret_encryption_key(*, required: bool = False) -> bytes | None:
    value = os.environ.get("RELAYCORE_SECRET_ENCRYPTION_KEY", "").strip()
    if not value:
        if required:
            raise RuntimeError("RELAYCORE_SECRET_ENCRYPTION_KEY is required for encrypted secret storage.")
        return None
    try:
        Fernet(value.encode())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("RELAYCORE_SECRET_ENCRYPTION_KEY must be a valid Fernet key.") from exc
    return value.encode()


OIDC_SETTINGS = oidc_settings()

