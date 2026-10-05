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


class SlackError(Exception):
    pass


def slack_settings() -> dict[str, str] | None:
    names = {
        "client_id": "RELAYCORE_SLACK_CLIENT_ID",
        "client_secret": "RELAYCORE_SLACK_CLIENT_SECRET",
        "app_id": "RELAYCORE_SLACK_APP_ID",
        "signing_secret": "RELAYCORE_SLACK_SIGNING_SECRET",
        "callback_url": "RELAYCORE_SLACK_CALLBACK_URL",
    }
    values = {key: os.environ.get(name, "").strip() for key, name in names.items()}
    if not any(values.values()):
        return None
    missing = [names[key] for key, value in values.items() if not value]
    if missing:
        raise RuntimeError("Incomplete Slack App configuration; missing " + ", ".join(missing) + ".")
    if len(values["client_secret"]) < 16 or len(values["signing_secret"]) < 32:
        raise RuntimeError("Slack client and signing secrets are too short.")
    if not re.fullmatch(r"A[A-Z0-9]{1,24}", values["app_id"]):
        raise RuntimeError("RELAYCORE_SLACK_APP_ID is invalid.")
    parsed = urlsplit(values["callback_url"])
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not parsed.path.endswith("/integrations/slack/callback")):
        raise RuntimeError("RELAYCORE_SLACK_CALLBACK_URL must be an HTTPS callback URL.")
    return values


def authorization_url(config: dict[str, str], state: str) -> str:
    return "https://slack.com/oauth/v2/authorize?" + urlencode({
        "client_id": config["client_id"], "redirect_uri": config["callback_url"],
        "scope": "app_mentions:read", "state": state,
    })


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def exchange_code(config: dict[str, str], code: str) -> dict[str, Any]:
    authorization = base64.b64encode(f"{config['client_id']}:{config['client_secret']}".encode()).decode()
    request = Request(
        "https://slack.com/api/oauth.v2.access",
        data=urlencode({"code": code, "redirect_uri": config["callback_url"]}).encode(),
        headers={"Accept": "application/json", "Authorization": f"Basic {authorization}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with build_opener(_NoRedirect).open(request, timeout=8) as response:
            raw = response.read(1_000_001)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise SlackError("Slack authorization request failed.") from exc
    if len(raw) > 1_000_000:
        raise SlackError("Slack response exceeded the size limit.")
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SlackError("Slack returned an invalid response.") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise SlackError("Slack authorization could not be verified.")
    return result


def verify_request(raw_body: bytes, timestamp: str | None, signature: str | None,
                   secret: str, now: int | None = None) -> bool:
    if not timestamp or not re.fullmatch(r"[0-9]{1,12}", timestamp):
        return False
    if not signature or not re.fullmatch(r"v0=[0-9a-fA-F]{64}", signature):
        return False
    if abs((now if now is not None else int(time.time())) - int(timestamp)) > 300:
        return False
    base = b"v0:" + timestamp.encode("ascii") + b":" + raw_body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


def normalize_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    if payload.get("type") != "event_callback":
        return None
    team_id = payload.get("team_id")
    event_id = payload.get("event_id")
    event = payload.get("event")
    if (not isinstance(team_id, str) or not 1 <= len(team_id) <= 64
            or not isinstance(event_id, str) or not 1 <= len(event_id) <= 128
            or not isinstance(event, dict)):
        raise ValueError("Slack event is missing required fields.")
    event_type = event.get("type")
    if event_type == "app_uninstalled":
        return {"type": "slack.app_uninstalled", "team": {"id": team_id}}
    if event_type != "app_mention":
        return None
    channel, user, ts, message = event.get("channel"), event.get("user"), event.get("ts"), event.get("text", "")
    if (not isinstance(channel, str) or not channel or len(channel) > 64
            or not isinstance(user, str) or not user or len(user) > 64
            or not isinstance(ts, str) or not ts or len(ts) > 32
            or not isinstance(message, str) or len(message) > 4000):
        raise ValueError("Slack mention is missing required fields.")
    return {"type": "slack.app_mention", "team": {"id": team_id},
            "event": {"type": "app_mention", "channel": channel, "user": user,
                      "ts": ts, "text": message}}
