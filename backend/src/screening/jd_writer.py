"""Gate 1: the hiring manager's brief -> AI draft of the JD + role questions -> manager approves.

    draft_jd()    brief -> {job, questions, language_notes}; saved as <data>/jd_draft.json.
                  Nothing is live yet.
    approve_jd()  the (edited) draft is validated and posted as a new job (jobs.py), with
                  its own candidates. Role questions (kind: role) are new; the logistics
                  questions are copied from the default job.
"""

from __future__ import annotations

import json
import re
from datetime import date

from pydantic import ValidationError

from .config import AppConfig, ConfigError, JobDescription, ScreeningQuestion, ScreeningQuestions
from .llm import LLMClient
from .prompts import load_prompt
from .schemas import JDDraftLLM, utc_now
from .storage import write_json_atomic

PROMPT_DIR = "jd_writer"
MAX_BRIEF_CHARS = 6000
MAX_DOCUMENT_CHARS = 15000   # an uploaded job description can be longer than a typed brief
DOCUMENT_NOTE = ("The hiring manager uploaded an existing job description. Keep its facts (title, location, "
                 "experience range, skills, responsibilities) and tidy the wording:\n\n")


class JDError(ValueError):
    """The brief or the edited draft is not acceptable."""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "job"


def _draft_path(config: AppConfig):
    return config.data.root / "jd_draft.json"


def _existing_company(config: AppConfig) -> str | None:
    try:
        return config.job.company_name
    except ConfigError:
        return None


def merge_questions(current: list[ScreeningQuestion], role: list[str]) -> list[ScreeningQuestion]:
    """Keep every logistics question in place; the new role questions go where the old
    ones were (right after the first question, if there were none)."""
    new_role = [ScreeningQuestion(id=f"role_{i}", question=q.strip(), kind="role") for i, q in enumerate(role, 1)
                if q.strip()]
    logistics = [q for q in current if q.kind != "role"]
    first_role = next((i for i, q in enumerate(current) if q.kind == "role"), None)
    at = min(first_role, len(logistics)) if first_role is not None else min(1, len(logistics))
    return logistics[:at] + new_role + logistics[at:]


def draft_jd(config: AppConfig, llm: LLMClient, brief: str, *, company_name: str | None = None,
             from_document: bool = False) -> dict:
    """`from_document`: `brief` is the text of an uploaded job description, converted rather than written."""
    brief = (brief or "").strip()
    limit = MAX_DOCUMENT_CHARS if from_document else MAX_BRIEF_CHARS
    if len(brief) < 20:
        raise JDError("the document has almost no text" if from_document else
                      "describe the role in at least a sentence or two (what the person will do, key skills)")
    if len(brief) > limit:
        if not from_document:
            raise JDError(f"the brief is too long ({len(brief)} characters, max {MAX_BRIEF_CHARS})")
        brief = brief[:limit]
    company = (company_name or "").strip() or _existing_company(config) or "our company"
    system = load_prompt(PROMPT_DIR, "system.md")
    template = load_prompt(PROMPT_DIR, "draft_jd.md")
    out = llm.generate_json(system=system.text, prompt=template.render(
        brief=(DOCUMENT_NOTE + brief) if from_document else brief, company_name=company, today=date.today().isoformat()), schema=JDDraftLLM)

    job = {
        "job_id": f"{slug(out.title)}-{date.today():%Y%m%d}", "title": out.title.strip(), "company_name": company,
        "location": out.location, "min_experience_years": out.min_experience_years,
        "max_experience_years": out.max_experience_years,
        "must_have_skills": [s.strip() for s in out.must_have_skills if s.strip()],
        "nice_to_have_skills": [s.strip() for s in out.nice_to_have_skills if s.strip()],
        "description": out.description.strip() + "\n",
    }
    try:  # keep the public job posting link of the current JD
        if config.job.apply_url:
            job["apply_url"] = config.job.apply_url
    except ConfigError:
        pass
    try:
        current = config.questions.questions
    except ConfigError:
        current = []
    questions = merge_questions(current, [q.question for q in out.role_questions[:2]])
    draft = {"created_at": utc_now(), "brief": brief, "from_document": from_document, "llm": f"{llm.provider}/{llm.model}",
             "job": job, "questions": [q.model_dump() for q in questions], "language_notes": out.language_notes}
    write_json_atomic(_draft_path(config), draft)
    return draft


def load_draft(config: AppConfig) -> dict | None:
    p = _draft_path(config)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError):
        return None


KEEP = object()  # approve_jd(deadline=KEEP): leave an existing job's deadline as it is


def approve_jd(config: AppConfig, job: dict, questions: list[dict], *, by: str,
               scored_candidates: int = 0, job_id: str | None = None, deadline=KEEP) -> dict:
    """Validate the (edited) draft and post it as a new job (or, with job_id, update that job).
    Candidates already scored against an updated job are not re-scored."""
    from .jobs import JobError

    by = (by or "").strip()
    if not by or len(by) > 80:
        raise JDError("say who is approving (a name, up to 80 characters)")
    try:
        jd = JobDescription.model_validate({**job, "job_id": job_id or job.get("job_id") or "draft",
                                            "approved_by": by, "approved_at": utc_now()})
        qs = ScreeningQuestions.model_validate({"questions": questions})
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:6])
        raise JDError(f"the draft isn't valid yet: {errs}") from None
    if jd.min_experience_years is not None and jd.max_experience_years is not None             and jd.min_experience_years > jd.max_experience_years:
        raise JDError("minimum experience is above the maximum")

    fields = jd.model_dump(exclude_none=True)
    qlist = [q.model_dump() for q in qs.questions]
    try:
        if job_id:
            rec = config.jobs.update(job_id, fields, qlist, **({} if deadline is KEEP else {"deadline": deadline}))
        else:
            if not job.get("job_id"):
                fields.pop("job_id")
            rec = config.jobs.create(fields, qlist, posted_by=by, deadline=None if deadline is KEEP else deadline)
    except JobError as e:
        raise JDError(str(e)) from None
    _draft_path(config).unlink(missing_ok=True)
    return {"job_id": rec.job["job_id"], "title": jd.title, "questions": len(qs.questions),
            "updated": bool(job_id), "already_scored": scored_candidates if job_id else 0}
