"""Email verification before a screening call starts.

The interview link alone is not enough to start a call when
`stage3.verify_email` is on and the resume has an email address: the page asks
for a one-time code sent to that address. A correct code returns an *access key*
the page sends with `start`; it lives only in that browser tab, so a forwarded
link is useless without the candidate's inbox.

Only salted SHA-256 hashes of the code and access key are stored.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from ..config import AppConfig
from ..mailer import EmailError, Mailer, mask_email, render_email, valid_email
from ..schemas import utc_now
from .invites import Invite, InviteStore
from .notify import first_name


class VerificationError(Exception):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


def _digest(token: str, secret: str) -> str:
    return hashlib.sha256(f"{token}:{secret}".encode()).hexdigest()


def verification_required(config: AppConfig, email: str | None) -> bool:
    return config.settings.stage3.verify_email and valid_email(email)


def send_code(config: AppConfig, invites: InviteStore, inv: Invite, *, name: str | None, email: str,
              mailer: Mailer, now: datetime | None = None) -> dict:
    s3 = config.settings.stage3
    now = now or datetime.now(timezone.utc)
    if inv.otp_sent_at:
        wait = s3.otp_resend_seconds - (now - datetime.fromisoformat(inv.otp_sent_at)).total_seconds()
        if wait > 0:
            raise VerificationError(f"A code was just sent. You can ask for a new one in {int(wait) + 1} seconds.", 429)
    code = "".join(secrets.choice("0123456789") for _ in range(s3.otp_length))
    job = config.job
    mail = render_email("otp", to=email, subject=f"Your verification code: {code}", values={
        "first_name": first_name(name), "job_title": job.title, "company_name": job.company_name,
        "code": code, "minutes": str(s3.otp_expiry_minutes)})
    try:
        mailer.send(mail)
    except EmailError as e:
        raise VerificationError("We couldn't send the code right now. Please try again in a few minutes.", 503) from e
    invites.update(inv.token, otp_hash=_digest(inv.token, code), otp_sent_at=now.isoformat(timespec="seconds"),
                   otp_expires_at=(now + timedelta(minutes=s3.otp_expiry_minutes)).isoformat(timespec="seconds"),
                   otp_attempts=0)
    return {"sent_to": mask_email(email), "expires_in_minutes": s3.otp_expiry_minutes}


def check_code(config: AppConfig, invites: InviteStore, inv: Invite, code: str,
               now: datetime | None = None) -> str:
    """Returns a fresh access key if `code` is right. Raises VerificationError otherwise."""
    s3 = config.settings.stage3
    now = now or datetime.now(timezone.utc)
    code = (code or "").strip().replace(" ", "")
    if not inv.otp_hash or not inv.otp_expires_at:
        raise VerificationError("Please request a code first.", 400)
    if now >= datetime.fromisoformat(inv.otp_expires_at):
        raise VerificationError("That code has expired. Please request a new one.", 400)
    if inv.otp_attempts >= s3.otp_max_attempts:
        raise VerificationError("Too many attempts. Please request a new code.", 429)
    if not code.isdigit() or not hmac.compare_digest(_digest(inv.token, code), inv.otp_hash):
        attempts = inv.otp_attempts + 1
        invites.update(inv.token, otp_attempts=attempts)
        left = s3.otp_max_attempts - attempts
        raise VerificationError("That code isn't right. " + (f"{left} attempt{'s' if left != 1 else ''} left."
                                                             if left > 0 else "Please request a new code."), 400)
    key = secrets.token_urlsafe(24)
    invites.update(inv.token, access_key_hash=_digest(inv.token, key), verified_at=utc_now(),
                   otp_hash=None, otp_expires_at=None, otp_attempts=0)
    return key


def has_access(inv: Invite, access_key: str | None) -> bool:
    return bool(access_key and inv.access_key_hash
                and hmac.compare_digest(_digest(inv.token, access_key), inv.access_key_hash))
