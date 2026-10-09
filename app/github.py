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

from app.http_action import PermanentActionError, RetryableActionError


_EVENT_REFERENCE = "$event"
_REPOSITORY_SLUG = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100})$")


class GitHubError(Exception):
    pass


class GitHubHTTPError(GitHubError):
    def __init__(self, status: int, retry_after: float | None, rate_limited: bool):
        super().__init__(f"GitHub returned HTTP {status}.")
        self.status = status
        self.retry_after = retry_after
        self.rate_limited = rate_limited


class GitHubTransportError(GitHubError):
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


def _github_request(url: str, *, method: str, headers: dict[str, str], body: bytes | None = None,
                    timeout: float = 8) -> dict[str, Any]:
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(1_000_001)
    except HTTPError as exc:
        retry_after = exc.headers.get("Retry-After")
        try:
            delay = max(0.0, min(float(retry_after), 3600.0)) if retry_after is not None else None
        except (TypeError, ValueError):
            delay = None
        primary_rate_limit = exc.headers.get("X-RateLimit-Remaining") == "0"
        secondary_rate_limit = False
        if exc.code == 403 and not primary_rate_limit and retry_after is None:
            try:
                body = json.loads(exc.read(16_385))
                message = body.get("message", "") if isinstance(body, dict) else ""
                secondary_rate_limit = isinstance(message, str) and "secondary rate limit" in message.casefold()
            except (OSError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        rate_limited = primary_rate_limit or secondary_rate_limit or exc.code == 429 or retry_after is not None
        if delay is None and primary_rate_limit:
            try:
                reset_at = float(exc.headers.get("X-RateLimit-Reset", ""))
                delay = max(0.0, min(reset_at - time.time(), 3600.0))
            except (TypeError, ValueError):
                pass
        if delay is None and rate_limited:
            # GitHub recommends waiting at least one minute when a secondary limit has no Retry-After header.
            delay = 60.0
        exc.close()
        raise GitHubHTTPError(exc.code, delay, rate_limited) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise GitHubTransportError("GitHub request failed.") from exc
    if len(raw) > 1_000_000:
        raise GitHubError("GitHub response exceeded the size limit.")
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitHubError("GitHub returned an invalid response.") from exc
    if not isinstance(result, dict):
        raise GitHubError("GitHub returned an invalid response.")
    return result


def _repository_slug(value: object) -> tuple[str, str]:
    match = _REPOSITORY_SLUG.fullmatch(value) if isinstance(value, str) else None
    if not match or match.group(2) in {".", ".."}:
        raise ValueError("GitHub issue comments require an owner/repository name.")
    return match.group(1), match.group(2)


def _event_pointer(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    pointer = value.get(_EVENT_REFERENCE)
    if (set(value) != {_EVENT_REFERENCE} or not isinstance(pointer, str) or not pointer.startswith("/")
            or len(pointer) > 512 or re.search(r"~(?![01])", pointer)):
        raise ValueError("GitHub action event references must be {$event: '/json/pointer'} objects.")
    return pointer


def _validate_issue_number(value: object) -> None:
    if type(value) is not int or not 0 < value <= 2_147_483_647:
        raise ValueError("GitHub issue_number must be a positive 32-bit integer or an event reference.")


def _validate_comment_body(value: object) -> None:
    if (not isinstance(value, str) or not value.strip() or len(value) > 4000
            or any((ord(char) < 32 and char not in "\n\r\t") or ord(char) == 127 for char in value)):
        raise ValueError("GitHub comment body must contain 1–4000 printable characters or an event reference.")


def validate_issue_comment_step(payload: dict[str, Any]) -> None:
    if set(payload) != {"repository", "issue_number", "body"}:
        raise ValueError("GitHub issue comments require repository, issue_number, body, and no other fields.")
    repository_reference = _event_pointer(payload["repository"])
    if repository_reference is None:
        _repository_slug(payload["repository"])
    number_reference = _event_pointer(payload["issue_number"])
    if number_reference is None:
        _validate_issue_number(payload["issue_number"])
    body_reference = _event_pointer(payload["body"])
    if body_reference is None:
        _validate_comment_body(payload["body"])


def _event_value(payload: dict[str, Any] | None, reference: object, field: str) -> Any:
    pointer = _event_pointer(reference)
    if pointer is None:
        return reference
    if not isinstance(payload, dict):
        raise PermanentActionError("GitHub action requires a webhook event payload.")
    current: Any = payload
    for part in pointer[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and (part == "0" or not part.startswith("0")):
            index = int(part)
            if index < len(current):
                current = current[index]
            else:
                raise PermanentActionError(f"GitHub action {field} event field was not present.")
        else:
            raise PermanentActionError(f"GitHub action {field} event field was not present.")
    return current


def _raise_github_action_error(error: GitHubError) -> None:
    if isinstance(error, GitHubTransportError):
        raise RetryableActionError("GitHub request failed or timed out.") from None
    if isinstance(error, GitHubHTTPError):
        retryable = error.status in {408, 429} or error.status >= 500 or (error.status == 403 and error.rate_limited)
        if retryable:
            raise RetryableActionError("GitHub temporarily rejected the action.",
                                       retry_after=error.retry_after) from None
        if error.status in {401, 403, 404, 422}:
            raise PermanentActionError(
                "GitHub denied the action; verify the linked installation, repository access, and Issues write permission."
            ) from None
        raise PermanentActionError(f"GitHub rejected the action with HTTP {error.status}.") from None
    raise PermanentActionError("GitHub returned an invalid action response.") from None


def execute_issue_comment_action(config: dict[str, Any], installation: dict[str, Any], payload: dict[str, Any],
                                 *, event_payload: dict[str, Any] | None = None,
                                 timeout_seconds: float = 8) -> dict[str, Any]:
    validate_issue_comment_step(payload)
    repository = _event_value(event_payload, payload["repository"], "repository")
    issue_number = _event_value(event_payload, payload["issue_number"], "issue_number")
    comment_body = _event_value(event_payload, payload["body"], "comment body")
    owner, repository_name = _repository_slug(repository)
    _validate_issue_number(issue_number)
    _validate_comment_body(comment_body)
    installation_id = installation.get("installation_id")
    account_login = installation.get("account_login")
    if (type(installation_id) is not int or installation_id <= 0 or installation.get("status") != "active"
            or not isinstance(account_login, str) or owner.casefold() != account_login.casefold()):
        raise PermanentActionError("GitHub repository does not belong to the active workspace installation.")

    deadline = time.monotonic() + timeout_seconds

    def remaining() -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise RetryableActionError("GitHub action timed out.")
        return seconds

    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "RelayCore"}
    try:
        token_response = _github_request(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens", method="POST",
            headers={**headers, "Authorization": f"Bearer {app_jwt(config)}"},
            body=json.dumps({"repositories": [repository_name], "permissions": {"issues": "write"}},
                            separators=(",", ":")).encode(),
            timeout=remaining(),
        )
        token = token_response.get("token")
        if (not isinstance(token, str) or not token.startswith("ghs_") or len(token) > 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in token)):
            raise GitHubError("GitHub returned an invalid installation token.")
        comment = _github_request(
            f"https://api.github.com/repos/{owner}/{repository_name}/issues/{issue_number}/comments",
            method="POST", headers={**headers, "Authorization": f"Bearer {token}",
                                     "Content-Type": "application/json"},
            body=json.dumps({"body": comment_body}, ensure_ascii=False, separators=(",", ":"),
                            allow_nan=False).encode(),
            timeout=remaining(),
        )
    except GitHubError as exc:
        _raise_github_action_error(exc)
    comment_id = comment.get("id")
    html_url = comment.get("html_url")
    if type(comment_id) is not int or comment_id <= 0 or not isinstance(html_url, str):
        raise PermanentActionError("GitHub returned an invalid issue comment.")
    url = urlsplit(html_url)
    valid_paths = {f"/{owner}/{repository_name}/issues/{issue_number}".casefold(),
                   f"/{owner}/{repository_name}/pull/{issue_number}".casefold()}
    if (url.scheme != "https" or url.hostname != "github.com" or url.username or url.password
            or url.path.casefold() not in valid_paths or url.query or url.fragment != f"issuecomment-{comment_id}"):
        raise PermanentActionError("GitHub returned an invalid issue-comment URL.")
    return {"comment_id": comment_id, "html_url": html_url}


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
