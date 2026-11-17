"""Signed Telegram authentication; no browser-supplied identity or Basic adapter."""

import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl

from fastapi import HTTPException, Request
from pydantic import Field
from rag._configuration import ProjectSettings
from rag.rag_api.mini_app_contracts import MiniAppIdentity
from rag.rag_api.config import get_settings, trusted_base_url

_windows = {}


def _rate_limit(user_id):
    # Bounded per-process safeguard; a public reverse proxy may impose a shared
    # limit across workers. Authentication always runs before allocating a key.
    now = time.monotonic()
    if len(_windows) >= 16000:
        for key, (started, _) in list(_windows.items()):
            if now - started >= 60:
                del _windows[key]
        if len(_windows) >= 16000 and user_id not in _windows:
            raise HTTPException(
                503, "rate_limiter_capacity", headers={"Retry-After": "60"}
            )
    started, count = _windows.get(user_id, (now, 0))
    if now - started >= 60:
        started, count = now, 0
    if count >= 120:
        raise HTTPException(
            429,
            "request_limit",
            headers={"Retry-After": str(max(1, int(60 - now + started)))},
        )
    _windows[user_id] = (started, count + 1)


class _MiniSettings(ProjectSettings):
    enabled: bool = Field(default=False, validation_alias="MINI_APP_ENABLED")
    url: str = Field(default="", validation_alias="MINI_APP_URL")
    admin_ids: str = Field(default="", validation_alias="MINI_APP_ADMIN_IDS")
    auth_max_age_s: int = Field(
        default=3600, ge=60, le=86400, validation_alias="MINI_APP_AUTH_MAX_AGE_S"
    )

    def _admins(self):
        values = self.admin_ids.split(",") if self.admin_ids.strip() else []
        if any(
            not v.strip().isdigit() or not 0 < int(v.strip()) < 2**63 for v in values
        ):
            raise ValueError("Invalid MINI_APP_ADMIN_IDS")
        return {int(v.strip()) for v in values}

    def _url(self):
        return trusted_base_url(self.url)


def _verify(raw, token, max_age, admins, now=None):
    try:
        if not raw or len(raw) > 16384:
            raise ValueError
        pairs = parse_qsl(
            raw, keep_blank_values=True, strict_parsing=True, max_num_fields=32
        )
        values = dict(pairs)
        if len(values) != len(pairs):
            raise ValueError
        supplied = values.pop("hash")
        if not re.fullmatch("[0-9a-f]{64}", supplied):
            raise ValueError
        checked = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, checked.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            raise ValueError
        timestamp = int(values["auth_date"])
        current = time.time() if now is None else now
        if timestamp > current + 30 or current - timestamp > max_age:
            raise HTTPException(401, "telegram_auth_expired")
        user = json.loads(values["user"])
        uid = user["id"]
        if type(uid) is not int or not 0 < uid < 2**63:
            raise ValueError
        username = user.get("username")
        if username is not None:
            if not isinstance(username, str) or not re.fullmatch(
                "[A-Za-z0-9_]{1,32}", username
            ):
                raise ValueError
            username = username.lower()
        return MiniAppIdentity(
            user_id=uid, username=username, role="admin" if uid in admins else "user"
        )
    except (KeyError, ValueError, TypeError, UnicodeError):
        raise HTTPException(401, "invalid_telegram_auth") from None


async def _identity(request: Request):
    settings = _MiniSettings()
    if not settings.enabled:
        raise HTTPException(404, "mini_app_disabled")
    scheme, _, raw = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "tma":
        raise HTTPException(401, "telegram_auth_required")
    identity = _verify(
        raw, get_settings().token, settings.auth_max_age_s, settings._admins()
    )
    _rate_limit(identity.user_id)
    return identity


def _admin(identity):
    if identity.role != "admin":
        raise HTTPException(403, "admin_required")


async def _admin_identity(request: Request):
    identity = await _identity(request)
    _admin(identity)
    return identity
