from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import time
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from uuid import UUID

MAX_RESPONSE_BYTES = 64 * 1024
MAX_TIMEOUT_SECONDS = 1.0
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_FORBIDDEN_HEADERS = {"authorization", "connection", "content-length", "cookie", "host", "idempotency-key",
                      "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}
_SENSITIVE_KEYS = {"access_token", "accesstoken", "api_key", "apikey", "authorization", "client_secret",
                   "clientsecret", "password", "private_key", "refresh_token", "refreshtoken", "secret",
                   "secret_key", "token"}


class PermanentActionError(RuntimeError):
    """An outbound action must go to the dead-letter queue without retrying."""


class RetryableActionError(RuntimeError):
    """An outbound action may succeed on a later attempt."""


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self) -> None:
        raw = socket.create_connection((self.address, self.port), self.timeout, self.source_address)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def _normalize_host(host: str) -> str:
    try:
        value = host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("HTTP action host is invalid.") from exc
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError("HTTP action host must be a DNS name.")
    if len(value) > 253 or any(not _HOST_LABEL.fullmatch(label) for label in value.split(".")):
        raise ValueError("HTTP action host is invalid.")
    if value.endswith((".localhost", ".local", ".internal", ".test", ".invalid")):
        raise ValueError("HTTP action host is not public.")
    return value


def allowed_http_hosts() -> frozenset[str]:
    raw = os.environ.get("RELAYCORE_HTTP_ALLOWED_HOSTS", "")
    try:
        return frozenset(_normalize_host(value.strip()) for value in raw.split(",") if value.strip())
    except ValueError as exc:
        raise RuntimeError("RELAYCORE_HTTP_ALLOWED_HOSTS contains an invalid DNS name.") from exc


def validate_http_host(host: str) -> str:
    value = _normalize_host(host)
    try:
        hosts = allowed_http_hosts()
    except RuntimeError as exc:
        raise ValueError("The deployment HTTP host allowlist is invalid.") from exc
    if value not in hosts:
        raise ValueError("HTTP host is not in the deployment allowlist.")
    return value


def validate_http_target(url: str) -> tuple[str, int, str]:
    if not isinstance(url, str) or len(url) > 2048 or not url or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ValueError("HTTP action URL is invalid.")
    try:
        parts = urlsplit(url)
        host = _normalize_host(parts.hostname or "")
        port = parts.port or 443
    except ValueError as exc:
        raise ValueError("HTTP action URL is invalid.") from exc
    if (parts.scheme.lower() != "https" or port != 443 or parts.username is not None or parts.password is not None
            or "\\" in url
            or parts.fragment):
        raise ValueError("HTTP actions require an HTTPS URL on port 443 without URL credentials or fragments.")
    validate_http_host(host)
    if any(any(word in key.lower().replace("-", "_") for word in
               ("auth", "token", "key", "secret", "password", "credential"))
           for key, _ in parse_qsl(parts.query, keep_blank_values=True)):
        raise ValueError("Place credentials in the encrypted credential field, not the URL query.")
    target = urlunsplit(("", "", parts.path or "/", parts.query, ""))
    return host, port, target


def validate_http_step(payload: dict) -> None:
    required = {"method", "url", "credential_id"}
    allowed = required | {"headers", "body", "timeout_seconds"}
    if required - payload.keys() or payload.keys() - allowed:
        raise ValueError("HTTP actions require method, url, credential_id and only supported options.")
    if not isinstance(payload["method"], str) or payload["method"] not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError("HTTP action method must be GET, POST, PUT, PATCH, or DELETE.")
    validate_http_target(payload["url"])
    try:
        UUID(str(payload["credential_id"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("HTTP action credential_id must be a UUID.") from exc
    headers = payload.get("headers", {})
    if not isinstance(headers, dict) or len(headers) > 16:
        raise ValueError("HTTP action headers must be an object with at most 16 entries.")
    for name, value in headers.items():
        if (not isinstance(name, str) or not _HEADER_NAME.fullmatch(name) or name.lower() in _FORBIDDEN_HEADERS
                or any(word in name.lower().replace("-", "_") for word in
                       ("auth", "token", "key", "secret", "password", "credential"))
                or not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError("HTTP action contains an invalid or reserved header.")
    if "body" in payload:
        try:
            json.dumps(payload["body"], ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("HTTP action body must be valid JSON.") from exc
    timeout = payload.get("timeout_seconds", MAX_TIMEOUT_SECONDS)
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not 0.1 <= timeout <= MAX_TIMEOUT_SECONDS):
        raise ValueError(f"HTTP action timeout_seconds must be between 0.1 and {MAX_TIMEOUT_SECONDS}.")


def _resolve_public_addresses(host: str, port: int) -> list[str]:
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise RetryableActionError("HTTP action DNS resolution failed.") from exc
    addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses:
        raise RetryableActionError("HTTP action DNS resolution returned no addresses.")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise PermanentActionError("HTTP action DNS returned an invalid address.") from exc
        if not address.is_global:
            raise PermanentActionError("HTTP action DNS resolved to a non-public address.")
    return addresses


def _redact_json(value, credential: str):
    if isinstance(value, dict):
        return {key: "[redacted]" if str(key).lower().replace("-", "_") in _SENSITIVE_KEYS
                else _redact_json(item, credential) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json(item, credential) for item in value]
    if isinstance(value, str) and credential:
        return value.replace(credential, "[redacted]")
    return value


def execute_http_action(payload: dict, credential: str, idempotency_key: str,
                        credential_host: str | None) -> dict:
    validate_http_step(payload)
    if not credential or any(ord(c) < 32 or ord(c) == 127 for c in credential):
        raise PermanentActionError("HTTP action credential is invalid.")
    host, port, target = validate_http_target(payload["url"])
    if host != credential_host:
        raise PermanentActionError("HTTP action host does not match the credential's bound host.")
    addresses = _resolve_public_addresses(host, port)
    timeout = min(float(payload.get("timeout_seconds", MAX_TIMEOUT_SECONDS)), MAX_TIMEOUT_SECONDS)
    body = None
    headers = {"Accept": "application/json", "Authorization": f"Bearer {credential}",
               "Idempotency-Key": idempotency_key, "User-Agent": "RelayCore/0.1"}
    if "body" in payload:
        body = json.dumps(payload["body"], ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        headers["Content-Type"] = "application/json"
    headers.update(payload.get("headers", {}))
    connection = _PinnedHTTPSConnection(host, port, addresses[0], timeout)
    try:
        connection.request(payload["method"], target, body=body, headers=headers)
        response = connection.getresponse()
        deadline = time.monotonic() + timeout
        chunks, size = [], 0
        while size <= MAX_RESPONSE_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            connection.sock.settimeout(remaining)
            chunk = response.read(min(8192, MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise PermanentActionError("HTTP action response exceeded the 64 KiB limit.")
    except PermanentActionError:
        raise
    except Exception as exc:
        if isinstance(exc, (PermanentActionError, RetryableActionError)):
            raise
        raise RetryableActionError("HTTP action request failed or timed out.") from None
    finally:
        connection.close()

    if 300 <= response.status < 400:
        raise PermanentActionError("HTTP action redirects are not followed.")
    if response.status in {408, 429} or response.status >= 500:
        raise RetryableActionError(f"HTTP action returned retryable status {response.status}.")
    if not 200 <= response.status < 300:
        raise PermanentActionError(f"HTTP action returned status {response.status}.")
    raw = b"".join(chunks)
    result = {"status_code": response.status, "response_bytes": size,
              "content_type": response.getheader("Content-Type", "")[:128]}
    if "json" in result["content_type"].lower():
        try:
            result["body"] = _redact_json(json.loads(raw), credential)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            result["body"] = None
    return result
