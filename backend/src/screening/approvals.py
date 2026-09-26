"""Human approval gates and the result emails that follow them.

    Stage 2 (AI suggests)  ->  gate "shortlist": the recruiter approves or overrides
                               -> shortlisted: interview link emailed (Stage 3)
                               -> rejected:    polite "resume_rejected" email
    Stage 4 (AI suggests)  ->  gate "final": the hiring manager approves or overrides
                               -> send_final_results: "selected" / "not_selected" emails

Nothing is emailed about a decision until a person has approved it, and every
decision records what the AI suggested, who decided, when, and whether it was
overridden. The final lists (shortlisted / rejected) are `final_results()`.
"""

from __future__ import annotations

import csv
import io

from .agent.notify import candidate_contact, first_name
from .config import ConfigError
from .context import RunContext
from .index import resume_decision
from .mailer import EmailError, Mailer, make_mailer, render_email, valid_email
from .schemas import CandidateEntry, Decision, Notification, Review, ReviewGate, utc_now

RESULT_SUBJECTS = {
    "resume_rejected": "Your application for {title} at {company}",
    "selected": "Good news about your application for {title}",
    "not_selected": "Your application for {title} at {company}",
}


class ApprovalError(ValueError):
    """The decision can't be recorded (unknown candidate, wrong stage, bad value)."""


def _decision(value) -> Decision:
    if value not in ("shortlisted", "rejected"):
        raise ApprovalError(f"decision must be 'shortlisted' or 'rejected' (got {value!r})")
    return value


def _by(by: str | None) -> str:
    by = (by or "").strip()
    if not by or len(by) > 80:
        raise ApprovalError("say who is approving (a name, up to 80 characters)")
    return by


# ---------------------------------------------------------------------------
# Queues
# ---------------------------------------------------------------------------

def awaiting_shortlist_review(entry: CandidateEntry) -> bool:
    return (entry.stages["stage2_shortlisting"].status == "success" and "shortlist" not in entry.reviews
            and not entry.fraud_blocked)


def awaiting_final_review(entry: CandidateEntry) -> bool:
    return (entry.stages["stage4_evaluation"].status == "success" and "final" not in entry.reviews
            and resume_decision(entry) == "shortlisted" and not entry.fraud_blocked)


BOARD_COLUMNS = ("applied", "resume_review", "interview", "final_review", "selected", "rejected", "fraud")


def board_column(entry: CandidateEntry) -> str:
    """Where the candidate sits on a job's board.

    applied        resume being read / scored (or that failed)
    resume_review  scored, waiting for a recruiter to approve the resume shortlist
    interview      invited to (or in) the screening call, or being evaluated
    final_review   interviewed and evaluated, waiting for the manager's decision
    selected / rejected   decided (rejected also covers the resume stage and opting out)
    fraud          stopped by the credibility checks
    """
    st = entry.stages
    if entry.fraud_blocked:
        return "fraud"
    final = entry.reviews.get("final")
    if final is not None:
        return "selected" if final.decision == "shortlisted" else "rejected"
    if awaiting_shortlist_review(entry) and st["stage3_calling"].status not in ("awaiting", "success"):
        return "resume_review"  # the AI's suggestion (shortlist or reject) waits for a recruiter
    if resume_decision(entry) == "rejected":
        return "rejected"
    if st["stage3_calling"].status == "skipped" and st["stage3_calling"].output_path:
        return "rejected"  # opted out during the call
    if awaiting_final_review(entry):
        return "final_review"
    if st["stage2_shortlisting"].status == "success":
        return "interview"
    return "applied"


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def approve_shortlist(ctx: RunContext, decisions: dict[str, str], *, by: str, note: str | None = None,
                      mailer: Mailer | None = None) -> dict:
    """Record the recruiter's resume-shortlist decisions, then act on them:
    shortlisted -> interview link + invite email; rejected -> rejection email."""
    from .stage3_call import run_stage3

    by = _by(by)
    index = ctx.index.reload()
    checked = {}
    for cid, value in decisions.items():
        entry = index.get(cid)  # KeyError for unknown ids
        if entry.fraud_blocked:
            raise ApprovalError(f"{entry.display_name or cid}: fraud detected. Review the credibility flags and "
                                "clear them first if they're innocent")
        s2 = entry.stages["stage2_shortlisting"]
        if s2.status != "success":
            raise ApprovalError(f"{entry.display_name or cid}: resume screening hasn't finished (status {s2.status})")
        checked[cid] = (_decision(value), s2.decision)
    now = utc_now()
    for cid, (decision, ai) in checked.items():
        index.set_review(cid, "shortlist", Review(decision=decision, ai_decision=ai, by=by, at=now, note=note))
        ctx.logger.info("approvals: shortlist candidate_id=%s %s by %s%s", cid, decision.upper(), by,
                        f" (AI suggested {ai})" if ai and ai != decision else "")

    shortlisted = {c for c, (d, _) in checked.items() if d == "shortlisted"}
    rejected = [c for c, (d, _) in checked.items() if d == "rejected"]
    invited = run_stage3(ctx, only=shortlisted, mailer=mailer).awaiting if shortlisted else []
    emails = {}
    if rejected and ctx.config.settings.approvals.email_resume_rejections:
        mailer = mailer or _mailer_or_none(ctx)
        for cid in rejected:
            emails[cid] = send_result(ctx, ctx.index.get(cid), "shortlist", "resume_rejected", mailer).status
    return {"recorded": len(checked), "invited": invited, "rejected": rejected, "emails": emails}


def approve_final(ctx: RunContext, decisions: dict[str, str], *, by: str, note: str | None = None) -> dict:
    """Record the hiring manager's final decisions. Emails go out with send_final_results().
    The manager has the last word: any evaluated candidate can be shortlisted or rejected whatever
    their score, and an earlier final decision can be changed (the change is logged)."""
    by = _by(by)
    index = ctx.index.reload()
    checked = {}
    for cid, value in decisions.items():
        entry = index.get(cid)
        if entry.fraud_blocked:
            raise ApprovalError(f"{entry.display_name or cid}: fraud detected. Review the credibility flags and "
                                "clear them first if they're innocent")
        s4 = entry.stages["stage4_evaluation"]
        if s4.status != "success":
            raise ApprovalError(f"{entry.display_name or cid}: the interview hasn't been evaluated yet")
        if resume_decision(entry) != "shortlisted":
            raise ApprovalError(f"{entry.display_name or cid}: was not shortlisted at the resume stage")
        checked[cid] = (_decision(value), s4.decision)
    now = utc_now()
    changed = []
    for cid, (decision, ai) in checked.items():
        before = index.get(cid).reviews.get("final")
        if before and before.decision != decision:
            changed.append(cid)
            note_ = f"changed from {before.decision} (by {before.by})" + (f"; {note}" if note else "")
        else:
            note_ = note
        index.set_review(cid, "final", Review(decision=decision, ai_decision=ai, by=by, at=now, note=note_))
        ctx.logger.info("approvals: final candidate_id=%s %s by %s%s%s", cid, decision.upper(), by,
                        f" (AI suggested {ai})" if ai and ai != decision else "",
                        f" (changed from {before.decision})" if cid in changed else "")
    return {"recorded": len(checked), "changed": changed}


def result_kind(decision: str) -> str:
    return "selected" if decision == "shortlisted" else "not_selected"


def send_final_results(ctx: RunContext, mailer: Mailer | None = None, *, only: set[str] | None = None) -> dict:
    """Email every approved final decision that hasn't been emailed successfully yet."""
    mailer = mailer or _mailer_or_none(ctx)
    out = {}
    for entry in ctx.index.reload().all():
        cid = entry.candidate_id
        final = entry.reviews.get("final")
        if final is None or (only and cid not in only):
            continue
        kind = result_kind(final.decision)
        prev = entry.notifications.get("final")
        if prev and prev.status in ("sent", "outbox") and prev.kind == kind:
            continue  # already told the candidate this decision (a changed decision is emailed again)
        out[cid] = send_result(ctx, entry, "final", kind, mailer).status
    return out


# ---------------------------------------------------------------------------
# Result emails
# ---------------------------------------------------------------------------

def _mailer_or_none(ctx: RunContext) -> Mailer | None:
    try:
        return make_mailer(ctx.config)
    except ConfigError as e:
        ctx.logger.warning("approvals: result emails cannot be sent: %s", e)
        return None


def send_result(ctx: RunContext, entry: CandidateEntry, gate: ReviewGate, kind: str,
                mailer: Mailer | None) -> Notification:
    job = ctx.config.for_job(entry.job_id).job
    cid = entry.candidate_id
    try:
        name, address = candidate_contact(ctx.config, entry)
    except Exception:
        name, address = entry.display_name, None
    if not valid_email(address):
        n = Notification(kind=kind, status="skipped", at=utc_now(), error="no valid email address in the resume")
    elif mailer is None:
        n = Notification(kind=kind, status="failed", to=address, at=utc_now(), error="email is not configured")
    else:
        subject = RESULT_SUBJECTS[kind].format(title=job.title, company=job.company_name)
        try:
            mail = render_email(kind, to=address, subject=subject, values={
                "first_name": first_name(name), "job_title": job.title, "company_name": job.company_name})
            mailer.send(mail)
            n = Notification(kind=kind, status="outbox" if mailer.mode == "outbox" else "sent", to=address,
                             at=utc_now())
        except EmailError as e:
            n = Notification(kind=kind, status="failed", to=address, at=utc_now(), error=str(e)[:500])
    ctx.index.set_notification(cid, gate, n)
    log = ctx.logger.error if n.status == "failed" else ctx.logger.info
    log("approvals: candidate_id=%s %s email %s%s", cid, kind, n.status, f" ({n.error})" if n.error else "")
    return n


# ---------------------------------------------------------------------------
# The output: final shortlisted / rejected lists
# ---------------------------------------------------------------------------

RESULT_COLUMNS = ["list", "name", "email", "final_score", "resume_score", "interview_score", "ai_suggestion",
                  "decision", "decided_by", "decided_at", "overridden", "needs_review", "email_status",
                  "candidate_id"]


def final_results(ctx: RunContext, job_id: str | None = None) -> dict[str, list[dict]]:
    """Everyone interviewed and evaluated (for one job, or all jobs), split into the final lists.
    `awaiting_approval` holds evaluated candidates the manager hasn't decided on yet."""
    lists: dict[str, list[dict]] = {"shortlisted": [], "rejected": [], "awaiting_approval": []}
    jobs = ctx.config.jobs
    for entry in ctx.index.reload().all():
        if job_id is not None and jobs.candidate_job(entry) != job_id:
            continue
        s4 = entry.stages["stage4_evaluation"]
        if s4.status != "success" or resume_decision(entry) != "shortlisted":
            continue
        rec = ctx.config.store.get_record(s4.output_path) or {}
        final = entry.reviews.get("final")
        try:
            _, email = candidate_contact(ctx.config, entry)
        except Exception:
            email = None
        note = entry.notifications.get("final")
        row = {
            "name": entry.display_name, "email": email, "final_score": s4.score,
            "resume_score": rec.get("resume_score"), "interview_score": rec.get("interview_score"),
            "ai_suggestion": s4.decision, "decision": final.decision if final else None,
            "decided_by": final.by if final else None, "decided_at": final.at if final else None,
            "overridden": final.overridden if final else False, "needs_review": bool(rec.get("needs_review")),
            "email_status": note.status if note else None, "email_kind": note.kind if note else None,
            "email_current": bool(final and note and note.status in ("sent", "outbox")
                                  and note.kind == result_kind(final.decision)),
            "below_threshold": s4.decision == "rejected", "candidate_id": entry.candidate_id,
        }
        key = final.decision if final else "awaiting_approval"
        lists[key].append({"list": key, **row})
    for rows in lists.values():
        rows.sort(key=lambda r: -(r["final_score"] or 0))
    return lists


def results_csv(lists: dict[str, list[dict]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=RESULT_COLUMNS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for key in ("shortlisted", "rejected", "awaiting_approval"):
        w.writerows(lists.get(key, []))
    return buf.getvalue()
