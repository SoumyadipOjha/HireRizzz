"""Cooling period: someone who has finished a screening call can't be screened again, for any job,
until `stage3.cooldown_days` (default 30) after that call. Matched on the email address in the
resume. A recruiter can let one application through anyway, with a reason (recorded)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .schemas import CoolingPeriod, FraudClearance, utc_now


def _dt(iso: str | None) -> datetime | None:
    try:
        d = datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None
    return d.replace(tzinfo=timezone.utc) if d and d.tzinfo is None else d


def _email(store, entry) -> str | None:
    st = entry.stages["stage1_extraction"]
    if st.status != "success" or not st.output_path:
        return None
    rec = store.get_record(st.output_path) or {}
    email = ((rec.get("extraction") or {}).get("email") or "").strip().lower()
    return email or None


def cooling_active(entry, now: datetime | None = None) -> bool:
    c = entry.cooling
    if c is None or c.cleared is not None:
        return False
    until = _dt(c.until)
    return until is not None and until > (now or datetime.now(timezone.utc))


def find_recent_screening(ctx, entry, now: datetime | None = None):
    """(other entry, screened_at) of the latest finished screening call with the same email within the
    cooling window, or None."""
    days = ctx.config.settings.stage3.cooldown_days
    if days <= 0:
        return None
    store = ctx.config.store
    email = _email(store, entry)
    if not email:
        return None
    now = now or datetime.now(timezone.utc)
    best = None
    for other in ctx.index.all():
        if other.candidate_id == entry.candidate_id:
            continue
        s3 = other.stages["stage3_calling"]
        at = _dt(s3.updated_at)
        if s3.status != "success" or at is None or now - at >= timedelta(days=days):
            continue
        if _email(store, other) != email:
            continue
        if best is None or at > best[1]:
            best = (other, at)
    return best


def check_cooldown(ctx, candidate_id: str, now: datetime | None = None):
    """Put the candidate in a cooling period if their email finished a screening call recently.
    A recruiter's clearance is kept. Returns the entry."""
    entry = ctx.index.get(candidate_id)
    if entry.cooling and entry.cooling.cleared:
        return entry
    hit = find_recent_screening(ctx, entry, now)
    if hit is None:
        if entry.cooling and not cooling_active(entry, now):
            return entry  # an expired period stays on record, harmlessly
        return entry
    other, at = hit
    days = ctx.config.settings.stage3.cooldown_days
    jid = ctx.config.jobs.candidate_job(other)
    try:
        title = ctx.config.for_job(jid).job.title
    except Exception:
        title = jid
    period = CoolingPeriod(since=at.isoformat(timespec="seconds"),
                           until=(at + timedelta(days=days)).isoformat(timespec="seconds"),
                           previous_candidate_id=other.candidate_id, previous_job_id=jid, previous_job_title=title)
    if entry.cooling == period:
        return entry
    ctx.logger.info("cooldown: candidate_id=%s in cooling period until %s (screened for %s as candidate_id=%s)",
                    candidate_id, period.until, title, other.candidate_id)
    return ctx.index.set_cooling(candidate_id, period)


def clear_cooling(ctx, candidate_id: str, *, by: str, note: str | None = None):
    by = (by or "").strip()
    if not by or len(by) > 80:
        raise ValueError("say who is allowing this (a name, up to 80 characters)")
    entry = ctx.index.get(candidate_id)
    if not cooling_active(entry):
        raise ValueError("this candidate isn't in a cooling period")
    if not (note or "").strip():
        raise ValueError("say why this candidate may be screened again")
    ctx.logger.info("cooldown: candidate_id=%s cooling period LIFTED by %s: %s", candidate_id, by, note)
    cleared = entry.cooling.model_copy(update={"cleared": FraudClearance(by=by, at=utc_now(), note=note.strip()[:300])})
    return ctx.index.set_cooling(candidate_id, cleared)


def cooling_note(entry) -> str:
    c = entry.cooling
    until = (_dt(c.until) or datetime.now(timezone.utc)).strftime("%d %b %Y")
    return f"cooling period until {until}: this email finished a screening call for {c.previous_job_title} recently"
