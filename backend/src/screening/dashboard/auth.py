"""Dashboard login: one account, signed session tokens.

The username and password below are the defaults. DASHBOARD_USERNAME / DASHBOARD_PASSWORD
(environment) replace them without a code change. Tokens are signed with SESSION_SECRET,
or, if that isn't set, a key derived from the password, so changing the password signs
everyone out.

Candidates' interview pages don't use this: their private link is their access.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "HireRizz@Kanerika#2026"
TOKEN_TTL_SECONDS = 7 * 24 * 3600
AUTH_ENV = "DASHBOARD_AUTH"   # "off" disables the login (tests, local experiments)


def auth_enabled() -> bool:
    return os.environ.get(AUTH_ENV, "").strip().lower() not in ("off", "0", "false", "no")


def _credentials() -> tuple[str, str]:
    return (os.environ.get("DASHBOARD_USERNAME", "").strip() or DEFAULT_USERNAME,
            os.environ.get("DASHBOARD_PASSWORD", "") or DEFAULT_PASSWORD)


def _key() -> bytes:
    secret = os.environ.get("SESSION_SECRET", "").strip()
    if secret:
        return secret.encode()
    user, password = _credentials()
    return hashlib.sha256(f"hirerizz-session|{user}|{password}".encode()).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def check_login(username: str, password: str) -> str | None:
    """The signed-in username if the credentials are right, else None."""
    user, pw = _credentials()
    ok_user = hmac.compare_digest(str(username or "").strip().lower().encode(), user.lower().encode())
    ok_pw = hmac.compare_digest(str(password or "").encode(), pw.encode())
    return user if ok_user and ok_pw else None


def make_token(user: str, now: float | None = None) -> str:
    payload = _b64(json.dumps({"u": user, "exp": int((now or time.time()) + TOKEN_TTL_SECONDS)}).encode())
    sig = _b64(hmac.new(_key(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify_token(token: str | None, now: float | None = None) -> str | None:
    """The username in a valid, unexpired token, else None."""
    if not token or token.count(".") != 1:
        return None
    payload, sig = token.split(".")
    good = _b64(hmac.new(_key(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, good):
        return None
    try:
        data = json.loads(_unb64(payload))
    except ValueError:
        return None
    if data.get("exp", 0) < (now or time.time()) or data.get("u") != _credentials()[0]:
        return None
    return data["u"]
