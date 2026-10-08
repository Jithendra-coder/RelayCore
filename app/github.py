from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class GitHubError(Exception):
    pass


def github_settings() -> dict[str, Any] | None:
    names = {
        "app_slug": "RELAYCORE_GITHUB_APP_SLUG",
        "app_id": "RELAYCORE_GITHUB_APP_ID",
        "client_id": "RELAYCORE_GITHUB_CLIENT_ID",
        "client_secret": "RELAYCORE_GITHUB_CLIENT_SECRET",
        "private_key": "RELAYCORE_GITHUB_PRIVATE_KEY_B64",
        "webhook_secret": "RELAYCORE_GITHUB_WEBHOOK_SECRET",
        "setup_url": "RELAYCORE_GITHUB_SETUP_URL",
        "callback_url": "RELAYCORE_GITHUB_CALLBACK_URL",
    }
    values: dict[str, Any] = {key: os.environ.get(name, "").strip() for key, name in names.items()}
    if not any(values.values()):
        return None
    missing = [names[key] for key, value in values.items() if not value]
    if missing:
        raise RuntimeError("Incomplete GitHub App configuration; missing " + ", ".join(missing) + ".")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?", values["app_slug"]):
        raise RuntimeError("RELAYCORE_GITHUB_APP_SLUG is invalid.")
    if not values["app_id"].isdigit() or int(values["app_id"]) <= 0:
        raise RuntimeError("RELAYCORE_GITHUB_APP_ID must be a positive integer.")
    if len(values["client_secret"]) < 16 or len(values["webhook_secret"]) < 32:
        raise RuntimeError("GitHub client and webhook secrets are too short.")
    for key in ("setup_url", "callback_url"):
        parsed = urlsplit(values[key])
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise RuntimeError(f"{names[key]} must be an HTTPS URL without credentials, query, or fragment.")
    if not values["setup_url"].endswith("/integrations/github/setup"):
        raise RuntimeError("RELAYCORE_GITHUB_SETUP_URL must end in /integrations/github/setup.")
    if not values["callback_url"].endswith("/integrations/github/callback"):
        raise RuntimeError("RELAYCORE_GITHUB_CALLBACK_URL must end in /integrations/github/callback.")
    try:
        pem = base64.b64decode(values["private_key"], validate=True)
        private_key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("RELAYCORE_GITHUB_PRIVATE_KEY_B64 must be base64-encoded PEM.") from exc
    if not isinstance(private_key, rsa.RSAPrivateKey) or private_key.key_size < 2048:
        raise RuntimeError("The GitHub App private key must be RSA with at least 2048 bits.")
    values["app_id"] = int(values["app_id"])
    values["private_key"] = private_key
    return values


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    verifier = _b64url(os.urandom(48))
    return verifier, _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def pkce_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def installation_url(config: dict[str, Any], state: str) -> str:
    return f"https://github.com/apps/{config['app_slug']}/installations/new?{urlencode({'state': state})}"


def authorization_url(config: dict[str, Any], state: str, challenge: str) -> str:
    return "https://github.com/login/oauth/authorize?" + urlencode({
        "client_id": config["client_id"], "redirect_uri": config["callback_url"],
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })


def app_jwt(config: dict[str, Any], now: int | None = None) -> str:
    issued = (now if now is not None else int(time.time())) - 60
    header = _b64url(b'{"alg":"RS256","typ":"JWT"}')
    claims = _b64url(json.dumps({"iat": issued, "exp": issued + 540, "iss": config["app_id"]},
                                separators=(",", ":")).encode())
    unsigned = f"{header}.{claims}"
    signature = config["private_key"].sign(unsigned.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return unsigned + "." + _b64url(signature)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _github_request(url: str, *, method: str, headers: dict[str, str], body: bytes | None = None) -> dict[str, Any]:
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with build_opener(_NoRedirect).open(request, timeout=8) as response:
            raw = response.read(1_000_001)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise GitHubError("GitHub request failed.") from exc
    if len(raw) > 1_000_000:
        raise GitHubError("GitHub response exceeded the size limit.")
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitHubError("GitHub returned an invalid response.") from exc
    if not isinstance(result, dict):
        raise GitHubError("GitHub returned an invalid response.")
    return result


def exchange_code(config: dict[str, Any], code: str, verifier: str) -> str:
    result = _github_request("https://github.com/login/oauth/access_token", method="POST", headers={
        "Accept": "application/json", "Content-Type": "application/json",
    }, body=json.dumps({"client_id": config["client_id"], "client_secret": config["client_secret"],
                        "code": code, "redirect_uri": config["callback_url"],
                        "code_verifier": verifier}, separators=(",", ":")).encode())
    token = result.get("access_token")
    if not isinstance(token, str) or not token:
        raise GitHubError("GitHub authorization could not be verified.")
    return token


def user_installation(config: dict[str, Any], token: str, installation_id: int) -> dict[str, Any]:
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "RelayCore"}
    # ponytail: cap verification at 2,000 installations to bound callback latency; raise if enterprise use needs more.
    for page in range(1, 21):
        result = _github_request(f"https://api.github.com/user/installations?per_page=100&page={page}",
                                 method="GET", headers=headers)
        installations = result.get("installations")
        if not isinstance(installations, list):
            raise GitHubError("GitHub returned an invalid installation list.")
        for installation in installations:
            if (isinstance(installation, dict) and type(installation.get("id")) is int
                    and installation["id"] == installation_id):
                return installation
        total_count = result.get("total_count")
        if type(total_count) is not int or total_count <= page * 100 or not installations:
            break
    raise GitHubError("The signed-in GitHub user cannot access this installation.")


def app_installation(config: dict[str, Any], installation_id: int) -> dict[str, Any]:
    return _github_request(f"https://api.github.com/app/installations/{installation_id}", method="GET", headers={
        "Accept": "application/vnd.github+json", "Authorization": f"Bearer {app_jwt(config)}",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "RelayCore",
    })


def verify_webhook(raw_body: bytes, signature: str | None, secret: str) -> bool:
    if not signature or not re.fullmatch(r"sha256=[0-9a-fA-F]{64}", signature):
        return False
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:].lower(), digest)


def normalize_pull_request(payload: dict[str, Any], event_name: str) -> dict[str, Any] | None:
    if event_name != "pull_request":
        return None
    action = payload.get("action")
    if action not in {"opened", "reopened", "synchronize", "closed"}:
        return None
    installation = payload.get("installation")
    repository = payload.get("repository")
    pull_request = payload.get("pull_request")
    if (not isinstance(installation, dict) or type(installation.get("id")) is not int
            or not 0 < installation["id"] <= 9223372036854775807 or not isinstance(repository, dict)
            or type(repository.get("id")) is not int or not isinstance(repository.get("full_name"), str)
            or not isinstance(pull_request, dict) or type(pull_request.get("id")) is not int
            or type(pull_request.get("number")) is not int):
        raise ValueError("GitHub pull request event is missing required fields.")
    pr_fields = ("id", "number", "state", "title", "body", "html_url", "draft", "merged")
    repo_fields = ("id", "full_name", "html_url")
    sender = payload.get("sender")
    return {
        "type": f"github.pull_request.{action}", "action": action,
        "installation": {"id": installation["id"]},
        "repository": {key: repository.get(key) for key in repo_fields},
        "pull_request": {key: pull_request.get(key) for key in pr_fields},
        "sender": {key: sender.get(key) for key in ("id", "login")} if isinstance(sender, dict) else {},
    }
