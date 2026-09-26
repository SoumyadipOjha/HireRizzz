"""Private interview links (data/stage3_calls/invites.json or the MongoDB `invites` collection).

A token is an unguessable secret (secrets.token_urlsafe) that maps to one
candidate_id. Only the token appears in the URL — never the candidate_id or
any personal data. One active invite per candidate; creating a new one
revokes the old one.
"""

from __future__ import annotations

import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..schemas import utc_now
from ..storage import Store

InviteStatus = Literal["active", "used", "revoked"]
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")


class InviteError(Exception):
    """Link is unknown, expired, revoked or already used."""


class Invite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str
    candidate_id: str
    created_at: str
    expires_at: str
    status: InviteStatus = "active"
    sessions: list[str] = []
    updated_at: str | None = None

    def expired(self) -> bool:
        return datetime.now(timezone.utc) >= datetime.fromisoformat(self.expires_at)


class InviteStore:
    def __init__(self, store: Store):
        self.store = store
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Invite]:
        return {t: Invite.model_validate(v) for t, v in self.store.load_invites().items()}

    def _save(self, invites: dict[str, Invite], changed: list[str]) -> None:
        self.store.save_invites({t: i.model_dump() for t, i in invites.items()}, changed)

    def active_for(self, candidate_id: str) -> Invite | None:
        with self._lock:
            return next((i for i in self._load().values()
                         if i.candidate_id == candidate_id and i.status == "active" and not i.expired()), None)

    def create(self, candidate_id: str, ttl_days: int) -> Invite:
        with self._lock:
            invites = self._load()
            now = datetime.now(timezone.utc)
            changed = []
            for t, inv in invites.items():
                if inv.candidate_id == candidate_id and inv.status == "active":
                    inv.status, inv.updated_at = "revoked", utc_now()
                    changed.append(t)
            token = secrets.token_urlsafe(24)  # 32 chars, ~192 bits
            inv = Invite(token=token, candidate_id=candidate_id, created_at=now.isoformat(timespec="seconds"),
                         expires_at=(now + timedelta(days=ttl_days)).isoformat(timespec="seconds"))
            invites[token] = inv
            self._save(invites, changed + [token])
            return inv

    def validate(self, token: str) -> Invite:
        if not TOKEN_RE.match(token or ""):
            raise InviteError("invalid link")
        with self._lock:
            inv = self._load().get(token)
        if inv is None:
            raise InviteError("invalid link")
        if inv.status == "used":
            raise InviteError("this screening call has already been completed")
        if inv.status == "revoked":
            raise InviteError("this link is no longer active")
        if inv.expired():
            raise InviteError("this link has expired")
        return inv

    def update(self, token: str, *, status: InviteStatus | None = None, add_session: str | None = None) -> None:
        with self._lock:
            invites = self._load()
            inv = invites[token]
            if status:
                inv.status = status
            if add_session and add_session not in inv.sessions:
                inv.sessions.append(add_session)
            inv.updated_at = utc_now()
            self._save(invites, [token])
