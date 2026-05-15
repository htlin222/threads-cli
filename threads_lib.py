"""Threads API helper library.

Pure functions, typed errors, and HTTP wrappers. The CLI lives in
``threads.py`` and only orchestrates these helpers; tests live in
``tests/test_threads_lib.py`` and exercise this module directly.
"""

from __future__ import annotations

import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import requests

# ---------- Constants ----------

GRAPH = "https://graph.threads.net"
AUTH_URL = "https://threads.net/oauth/authorize"

SCOPES: tuple[str, ...] = (
    "threads_basic",
    "threads_content_publish",
    "threads_delete",
    "threads_manage_replies",
    "threads_read_replies",
    "threads_manage_insights",
    "threads_keyword_search",
    "threads_manage_mentions",
)

DEFAULT_USER_INSIGHTS_METRICS = "views,likes,replies,reposts,quotes,followers_count"
DEFAULT_POST_INSIGHTS_METRICS = "views,likes,replies,reposts,quotes,shares"

MAX_TEXT_LEN = 500
CAROUSEL_MIN = 2
CAROUSEL_MAX = 20

REPLY_CONTROL_VALUES = frozenset({
    "everyone", "accounts_you_follow", "mentioned_only",
    "parent_post_author_only", "followers_only",
})

# pattern for ISO-3166-1 alpha-2 country codes (2 uppercase letters)
COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2}$")


# ---------- Typed errors ----------

class ThreadsError(Exception):
    """Base class for everything raised by threads_lib."""


class ValidationError(ThreadsError):
    """A caller violated a precondition before we hit the API."""


@dataclass
class ApiError(ThreadsError):
    """HTTP / API call failed. Carries enough structure for callers to react."""

    status: int
    code: int | None
    subcode: int | None
    message: str
    raw: dict | None = None

    def __str__(self) -> str:
        bits = [f"HTTP {self.status}"]
        if self.code is not None:
            bits.append(f"code {self.code}")
        if self.subcode is not None:
            bits.append(f"subcode {self.subcode}")
        return f"[{' / '.join(bits)}] {self.message}"


class AuthError(ApiError):
    """401 / OAuth error. Caller can catch this specifically to attempt refresh."""


def _parse_error(r: requests.Response) -> ApiError:
    try:
        body = r.json()
    except ValueError:
        body = None
    err = body.get("error", {}) if isinstance(body, dict) else {}
    code = err.get("code")
    subcode = err.get("error_subcode")
    message = err.get("message") or (r.text[:500] if r.text else f"HTTP {r.status_code}")
    cls = AuthError if r.status_code == 401 or code == 190 else ApiError
    return cls(status=r.status_code, code=code, subcode=subcode, message=message, raw=body)


# ---------- Config ----------

@dataclass
class Config:
    """Read/write/merge .env. Falls back to env vars prefixed THREADS_."""

    env_path: Path
    data: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, env_path: Path) -> "Config":
        data: dict[str, str] = {}
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip().strip('"').strip("'")
        for k, v in os.environ.items():
            if k.startswith("THREADS_"):
                data[k] = v
        return cls(env_path=env_path, data=data)

    def save(self, updates: dict[str, str]) -> None:
        existing = (
            self.env_path.read_text().splitlines() if self.env_path.exists() else []
        )
        seen: set[str] = set()
        out: list[str] = []
        for line in existing:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k in updates:
                    out.append(f"{k}={updates[k]}")
                    seen.add(k)
                    continue
            out.append(line)
        for k, v in updates.items():
            if k not in seen:
                out.append(f"{k}={v}")
        self.env_path.write_text("\n".join(out).rstrip() + "\n")
        self.data.update(updates)

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.data.get(key, default)

    def require(self, *keys: str) -> tuple[str, ...]:
        missing = [k for k in keys if not self.data.get(k)]
        if missing:
            raise ValidationError(f"missing in .env: {', '.join(missing)}")
        return tuple(self.data[k] for k in keys)


# ---------- HTTP wrappers ----------

def gget(path: str, token: str | None, *, timeout: int = 30, **params) -> dict:
    if token is not None:
        params["access_token"] = token
    r = requests.get(f"{GRAPH}/v1.0{path}", params=params, timeout=timeout)
    if r.status_code != 200:
        raise _parse_error(r)
    return r.json()


def gpost(path: str, token: str, *, timeout: int = 30, **params) -> dict:
    params["access_token"] = token
    r = requests.post(f"{GRAPH}/v1.0{path}", data=params, timeout=timeout)
    if r.status_code != 200:
        raise _parse_error(r)
    return r.json()


def gdelete(path: str, token: str, *, timeout: int = 30) -> dict:
    r = requests.delete(
        f"{GRAPH}/v1.0{path}", params={"access_token": token}, timeout=timeout
    )
    if r.status_code != 200:
        raise _parse_error(r)
    return r.json()


# ---------- OAuth ----------

def build_auth_url(
    client_id: str,
    redirect_uri: str,
    scopes: Iterable[str] | str,
    state: str,
) -> str:
    scope_str = scopes if isinstance(scopes, str) else ",".join(scopes)
    return f"{AUTH_URL}?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope_str,
        "response_type": "code",
        "state": state,
    })


def extract_code(raw: str) -> str:
    """Pull the OAuth code out of either a redirect URL or a bare code string."""
    raw = raw.strip()
    if raw.startswith("http"):
        parsed = urllib.parse.urlparse(raw)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" not in params:
            raise ValidationError(f"URL has no ?code=: {raw}")
        raw = params["code"][0]
    # Threads sometimes appends a "#_" suffix to codes; strip it.
    return raw.split("#", 1)[0]


def poll_worker_for_code(
    worker_base: str,
    state: str,
    *,
    interval: float = 2.0,
    timeout: float = 300.0,
    now=time.monotonic,
    sleep=time.sleep,
) -> str:
    """Poll the Cloudflare Worker's /poll endpoint until the code appears.

    ``now`` and ``sleep`` are injectable for tests.
    """
    poll_url = f"{worker_base.rstrip('/')}/poll"
    deadline = now() + timeout
    while now() < deadline:
        try:
            r = requests.get(poll_url, params={"state": state}, timeout=10)
        except requests.RequestException:
            sleep(interval)
            continue
        if r.status_code == 200 and r.text:
            return r.text.strip().split("#", 1)[0]
        sleep(interval)
    raise ApiError(
        status=408, code=None, subcode=None,
        message=f"timed out waiting for OAuth code after {timeout:.0f}s",
    )


def exchange_code_for_short_token(cfg: Config, code: str) -> dict:
    client_id, client_secret, redirect_uri = cfg.require(
        "THREADS_CLIENT_ID", "THREADS_CLIENT_SECRET", "THREADS_REDIRECT_URI"
    )
    r = requests.post(
        f"{GRAPH}/oauth/access_token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise _parse_error(r)
    return r.json()


def exchange_to_long_lived(cfg: Config, short_token: str) -> dict:
    client_secret = cfg.require("THREADS_CLIENT_SECRET")[0]
    r = requests.get(
        f"{GRAPH}/access_token",
        params={
            "grant_type": "th_exchange_token",
            "client_secret": client_secret,
            "access_token": short_token,
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise _parse_error(r)
    return r.json()


def refresh_long_lived(cfg: Config, long_token: str) -> dict:
    r = requests.get(
        f"{GRAPH}/refresh_access_token",
        params={
            "grant_type": "th_refresh_token",
            "access_token": long_token,
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise _parse_error(r)
    return r.json()


def persist_token(
    cfg: Config,
    token: str,
    expires_in: int | None,
    user_id: str | None,
    *,
    now=time.time,
) -> None:
    updates = {"THREADS_ACCESS_TOKEN": token}
    if expires_in:
        updates["THREADS_TOKEN_EXPIRES_AT"] = str(int(now()) + int(expires_in))
    if user_id:
        updates["THREADS_USER_ID"] = str(user_id)
    cfg.save(updates)


def fmt_expiry(expires_at: str | int | None, *, now=time.time) -> str:
    if not expires_at:
        return "(unknown expiry)"
    remaining = int(expires_at) - int(now())
    if remaining <= 0:
        return f"EXPIRED {(-remaining) // 86400}d ago"
    return f"{remaining // 86400}d {(remaining % 86400) // 3600}h remaining"


# ---------- Validators ----------

def validate_text_length(text: str, *, max_len: int = MAX_TEXT_LEN) -> str:
    if len(text) > max_len:
        raise ValidationError(f"text is {len(text)} chars; max {max_len}")
    return text


def validate_country_codes(csv: str) -> str:
    """Accept comma-separated ISO-3166-1 alpha-2 codes; normalize to uppercase."""
    codes = [c.strip().upper() for c in csv.split(",") if c.strip()]
    bad = [c for c in codes if not COUNTRY_CODE_RE.match(c)]
    if bad:
        raise ValidationError(f"invalid ISO-3166-1 alpha-2 codes: {bad}")
    if not codes:
        raise ValidationError("country codes list is empty")
    return ",".join(codes)


def validate_url(url: str, *, label: str = "url") -> str:
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValidationError(f"{label} must start with http:// or https://: {url!r}")
    return url


def validate_topic(topic: str) -> str:
    if not (1 <= len(topic) <= 50):
        raise ValidationError(f"topic must be 1-50 chars, got {len(topic)}")
    return topic


def validate_reply_control(value: str) -> str:
    if value not in REPLY_CONTROL_VALUES:
        raise ValidationError(
            f"reply_control must be one of {sorted(REPLY_CONTROL_VALUES)}; got {value!r}"
        )
    return value


# ---------- Publishing helpers ----------

def infer_carousel_item_type(url: str) -> str:
    """Return 'VIDEO' for common video extensions, else 'IMAGE'."""
    lower = url.lower().split("?", 1)[0]
    return "VIDEO" if lower.endswith((".mp4", ".mov", ".m4v")) else "IMAGE"


def build_post_params(
    *,
    text: str = "",
    link: str | None = None,
    topic: str | None = None,
    alt_text: str | None = None,
    reply_to: str | None = None,
    quote: str | None = None,
    reply_control: str | None = None,
    countries: str | None = None,
    location: str | None = None,
    is_carousel_item: bool = False,
) -> dict[str, str]:
    """Translate post flags into validated Graph API params."""
    params: dict[str, str] = {}
    if text:
        params["text"] = validate_text_length(text)
    if link:
        params["link_attachment"] = validate_url(link, label="--link")
    if topic:
        params["topic_tag"] = validate_topic(topic)
    if alt_text:
        params["alt_text"] = alt_text
    if reply_to:
        params["reply_to_id"] = reply_to
    if quote:
        params["quote_post_id"] = quote
    if reply_control:
        params["reply_control"] = validate_reply_control(reply_control)
    if countries:
        params["allowlisted_country_codes"] = validate_country_codes(countries)
    if location:
        params["location_id"] = location
    if is_carousel_item:
        params["is_carousel_item"] = "true"
    return params


def wait_for_container_ready(
    container_id: str,
    token: str,
    *,
    timeout: float = 300.0,
    initial_interval: float = 3.0,
    max_interval: float = 15.0,
    on_status=None,
    now=time.monotonic,
    sleep=time.sleep,
) -> None:
    """Poll until container status=FINISHED. Exponential backoff up to max_interval."""
    deadline = now() + timeout
    interval = initial_interval
    last_status: str | None = None
    while now() < deadline:
        info = gget(f"/{container_id}", token, fields="status,error_message")
        status = info.get("status")
        if status != last_status and on_status:
            on_status(container_id, status)
        last_status = status
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise ApiError(
                status=200, code=None, subcode=None,
                message=f"container {container_id} status={status}: {info.get('error_message')}",
            )
        sleep(interval)
        interval = min(interval * 1.5, max_interval)
    raise ApiError(
        status=408, code=None, subcode=None,
        message=f"container {container_id} not FINISHED after {timeout:.0f}s",
    )
