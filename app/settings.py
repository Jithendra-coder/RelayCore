from __future__ import annotations

import json
import os
from typing import Any

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres@127.0.0.1:55432/postgres")
DEMO_MODE = os.environ.get("RELAYCORE_DEMO_MODE", "0").lower() in {"1", "true", "yes"}
WORKER_COUNT = int(os.environ.get("RELAYCORE_WORKERS", "2"))
MAX_ATTEMPTS = int(os.environ.get("RELAYCORE_MAX_ATTEMPTS", "3"))
MAX_QUEUE_DEPTH = int(os.environ.get("RELAYCORE_QUEUE_LIMIT", "1000"))
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RELAYCORE_RATE_LIMIT_PER_MINUTE", "600"))
LEASE_SECONDS = float(os.environ.get("RELAYCORE_LEASE_SECONDS", "4"))


def api_keys() -> dict[str, dict[str, str]]:
    raw = os.environ.get("RELAYCORE_API_KEYS")
    if not raw and DEMO_MODE:
        return {"demo-key-change-me-32": {"tenant_id": "demo", "role": "admin"}}
    try:
        keys: Any = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("RELAYCORE_API_KEYS must be a JSON object.") from exc
    if not isinstance(keys, dict) or not keys:
        raise RuntimeError("Set RELAYCORE_API_KEYS before starting RelayCore.")
    for secret, identity in keys.items():
        if len(secret) < 16 or not isinstance(identity, dict) or not identity.get("tenant_id"):
            raise RuntimeError("Each API key must be at least 16 characters with a tenant_id and role.")
        if identity.get("role") not in {"admin", "operator", "viewer"}:
            raise RuntimeError("API key role must be admin, operator, or viewer.")
    return keys

