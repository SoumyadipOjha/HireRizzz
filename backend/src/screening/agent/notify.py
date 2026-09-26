"""Candidate emails about the screening call: the invite (QR + button) and reminders.

An email problem never fails Stage 3: the link is still valid, the problem is
recorded on the invite (`email_error`) and in the index note, and HR can share
the link by hand or re-send with `screening call --resend`.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta, timezone

import segno

from ..config import AppConfig
from ..mailer import Email, EmailError, InlineImage, Mailer, mask_email, render_email, valid_email
from ..schemas import CandidateEntry, utc_now
from .invites import Invite, InviteStore

QR_CID = "screening-qr"


def interview_url(config: AppConfig, token: str) -> str:
    return f"{config.settings.stage3.public_base_url}/interview/{token}"


def call_minutes(config: AppConfig) -> int:
    return max(5, round(len(config.questions.questions) * 0.8))


def qr_png(url: str) -> bytes:
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=8, border=2)
    return buf.getvalue()


def candidate_contact(config: AppConfig, entry: CandidateEntry) -> tuple[str | None, str | None]:
    """(full name, email) from the Stage 1 profile; (display_name, None) if there is none."""
    from ..stage2_shortlist import load_stage1

    if entry.stages["stage1_extraction"].status != "success":
        return entry.display_name, None
    ex = load_stage1(entry, config.store).extraction
    return ex.full_name or entry.display_name, (ex.email or "").strip() or None


def first_name(name: str | None) -> str:
    return name.split()[0] if name and name.split() else "there"


def build_invite_email(config: AppConfig, *, kind: str, to: str, name: str | None, url: str,
                       expires_at: str) -> Email:
    job = config.job
    expires_on = datetime.fromisoformat(expires_at).strftime("%d %b %Y")
    subject = (f"Next step for {job.title} at {job.company_name}: your screening call" if kind == "invite"
               else f"Reminder: your screening call for {job.title}")
    return render_email(kind, to=to, subject=subject, values={
        "first_name": first_name(name), "job_title": job.title, "company_name": job.company_name,
        "link": url, "expires_on": expires_on, "minutes": str(call_minutes(config)), "qr_cid": QR_CID,
        "verify_note": ("Before the call starts we'll email you a one-time code to confirm it's you. "
                        if config.settings.stage3.verify_email else ""),
    }, images=[InlineImage(cid=QR_CID, data=qr_png(url), filename="screening-link-qr.png")])


def email_invite(config: AppConfig, invites: InviteStore, entry: CandidateEntry, inv: Invite, mailer: Mailer,
                 logger: logging.Logger, *, kind: str = "invite") -> tuple[bool, str]:
    """Send the invite (or a reminder) for `inv`. Returns (sent, short note for the index)."""
    cid = entry.candidate_id
    try:
        name, address = candidate_contact(config, entry)
    except Exception as e:  # unreadable Stage 1 record: treat like "no address"
        name, address = entry.display_name, None
        logger.warning("stage3_calling: candidate_id=%s cannot read contact details: %s", cid, e)
    if not valid_email(address):
        msg = "no valid email address in the resume"
        invites.update(inv.token, email_error=msg)
        logger.warning("stage3_calling: candidate_id=%s %s %s — share the link manually", cid, kind, msg)
        return False, f"{kind} NOT emailed ({msg}); share the link manually"
    url = interview_url(config, inv.token)
    try:
        mail = build_invite_email(config, kind=kind, to=address, name=name, url=url, expires_at=inv.expires_at)
        desc = mailer.send(mail)
    except EmailError as e:
        invites.update(inv.token, email_error=str(e)[:500])
        logger.error("stage3_calling: candidate_id=%s %s email FAILED: %s", cid, kind, e)
        return False, f"{kind} email FAILED ({e}); share the link manually or retry with --resend"
    now = utc_now()
    if kind == "invite":
        invites.update(inv.token, email_to=address, emailed_at=now, email_error=None)
    else:
        invites.update(inv.token, email_to=address, email_error=None, reminders_sent=inv.reminders_sent + 1,
                       last_reminded_at=now)
    logger.info("stage3_calling: candidate_id=%s %s %s", cid, kind, desc)
    where = "outbox" if mailer.mode == "outbox" else "emailed"
    return True, f"{kind} {where} to {mask_email(address)}"


def due_reminders(config: AppConfig, invites: list[Invite], now: datetime | None = None) -> list[Invite]:
    """Active, emailed invites whose candidate has not started a call, past the reminder delay."""
    ec = config.settings.email
    now = now or datetime.now(timezone.utc)
    gap = timedelta(hours=ec.reminder_after_hours)
    out = []
    for inv in invites:
        if inv.status != "active" or inv.expired() or not inv.emailed_at or inv.sessions:
            continue
        if inv.reminders_sent >= ec.max_reminders:
            continue
        last = datetime.fromisoformat(inv.last_reminded_at or inv.emailed_at)
        if now - last >= gap:
            out.append(inv)
    return out


def send_reminders(ctx, mailer: Mailer, now: datetime | None = None) -> list[str]:
    """Send every due reminder. Returns the candidate_ids reminded."""
    from ..index import CandidateIndex

    invites = InviteStore(ctx.config.store)
    due = due_reminders(ctx.config, invites.all(), now)
    if not due:
        return []
    index = CandidateIndex(ctx.config.store)  # own read-only copy: safe next to the server's threads
    sent = []
    for inv in due:
        try:
            entry = index.get(inv.candidate_id)
        except KeyError:
            continue
        ok, note = email_invite(ctx.config.for_job(entry.job_id), invites, entry, inv, mailer, ctx.logger, kind="reminder")
        if ok:
            sent.append(inv.candidate_id)
    if sent:
        ctx.logger.info("stage3_calling: %d reminder(s) sent", len(sent))
    return sent
