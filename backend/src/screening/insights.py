"""Numbers for the dashboard's home page: the funnel, outcomes, score spread, activity and a few
facts about how the AI and the people deciding work together. Read-only."""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from .approvals import BOARD_COLUMNS, board_column
from .schemas import CandidateEntry

DAYS = 14
HUMAN_MINUTES_PER_SCREEN = 30   # a recruiter's phone screen incl. notes: what each AI call replaces
HUMAN_MINUTES_PER_RESUME = 6    # reading and scoring one resume by hand


def _dt(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def compute(config, index_doc: dict) -> dict:
    store = config.store
    jobs = config.jobs.all()
    default = config.jobs.default_id() if jobs else None
    entries: list[CandidateEntry] = []
    for raw in (index_doc.get("candidates") or {}).values():
        try:
            entries.append(CandidateEntry.model_validate(raw))
        except ValueError:
            continue

    columns = Counter(board_column(e) for e in entries)
    per_job = Counter(e.job_id or default for e in entries)

    resume_scores = [round(e.stages["stage2_shortlisting"].score) for e in entries
                     if e.stages["stage2_shortlisting"].status == "success" and e.stages["stage2_shortlisting"].score is not None]
    final_scores = [round(e.stages["stage4_evaluation"].score, 1) for e in entries
                    if e.stages["stage4_evaluation"].status == "success" and e.stages["stage4_evaluation"].score is not None]

    # the people deciding vs the AI's suggestions
    reviews = [r for e in entries for r in e.reviews.values() if r.ai_decision]
    overrides = [r for r in reviews if r.decision != r.ai_decision]

    # time from applying to the final decision
    hours = []
    for e in entries:
        f = e.reviews.get("final")
        a, b = _dt(e.ingested_at), _dt(f.at if f else None)
        if a and b and b >= a:
            hours.append((b - a).total_seconds() / 3600)

    # the screening calls
    durations = []
    for e in entries:
        st = e.stages["stage3_calling"]
        if st.status == "success" and st.output_path:
            rec = store.get_record(st.output_path) or {}
            d = (rec.get("call") or {}).get("duration_seconds")
            if d:
                durations.append(d)

    # which checks caught the fraud
    fraud_checks: Counter = Counter()
    for e in entries:
        if e.fraud_blocked or (e.credibility and e.credibility.red):
            doc = store.get_record(store.ref("credibility", e.candidate_id)) or {}
            fraud_checks.update(f["check"] for f in doc.get("flags", []) if f.get("level") == "red")

    emails = sum(1 for e in entries for n in e.notifications.values() if n.status in ("sent", "outbox"))
    invites = sum(1 for e in entries if e.stages["stage3_calling"].status in ("awaiting", "success")
                  or e.stages["stage3_calling"].output_path)

    today = datetime.now(timezone.utc).date()
    days = [today - timedelta(days=i) for i in range(DAYS - 1, -1, -1)]
    applied = Counter((_dt(e.ingested_at) or datetime.min).date() for e in entries)
    decided = Counter((_dt(r.at) or datetime.min).date() for e in entries for r in e.reviews.values())

    interviews = sum(e.stages["stage3_calling"].status == "success" for e in entries)
    scored = len(resume_scores)
    return {
        "totals": {
            "candidates": len(entries), "jobs": len(jobs), "open_jobs": sum(j.status == "open" for j in jobs),
            "interviews": interviews, "selected": columns["selected"], "fraud": columns["fraud"],
            "need_review": columns["resume_review"] + columns["final_review"], "emails": emails, "invites": invites,
        },
        "columns": {c: columns[c] for c in BOARD_COLUMNS},
        "per_job": [{"job_id": j.job["job_id"], "title": j.job["title"], "status": j.status,
                     "candidates": per_job[j.job["job_id"]]} for j in jobs],
        "resume_scores": resume_scores,
        "final_scores": final_scores,
        "daily": [{"date": d.isoformat(), "applied": applied[d], "decided": decided[d]} for d in days],
        "ai": {"decisions": len(reviews), "agreed": len(reviews) - len(overrides), "overrides": len(overrides)},
        "median_hours_to_decision": round(statistics.median(hours), 1) if hours else None,
        "calls": {"count": len(durations), "avg_seconds": round(statistics.mean(durations)) if durations else None,
                  "total_seconds": round(sum(durations))},
        "fraud_checks": [{"check": c, "count": n} for c, n in fraud_checks.most_common(6)],
        "hours_saved": round((scored * HUMAN_MINUTES_PER_RESUME + interviews * HUMAN_MINUTES_PER_SCREEN) / 60, 1),
        "pass_marks": {"resume": config.settings.thresholds.shortlist_score,
                       "final": config.settings.evaluation.final_threshold},
        "assumptions": {"minutes_per_resume": HUMAN_MINUTES_PER_RESUME, "minutes_per_screen": HUMAN_MINUTES_PER_SCREEN},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "today": date.today().isoformat(),
    }
