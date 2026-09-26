"""Gate 1: the hiring manager's brief -> AI draft of the JD + role questions -> manager approves.

    draft_jd()    brief -> {job, questions, language_notes}; saved as <data>/jd_draft.json.
                  Nothing is live yet.
    approve_jd()  the (edited) draft is validated and becomes config/job_description.yaml
                  and config/screening_questions.yaml. The previous files are kept in
                  config/history/. Role questions (kind: role) are replaced; logistics
                  questions stay as they are.

Candidates already scored against the old JD are not re-scored automatically:
`approve_jd` reports how many there are so the caller can warn about it.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import date, datetime, timezone

import yaml
from pydantic import ValidationError

from .config import AppConfig, ConfigError, JobDescription, ScreeningQuestion, ScreeningQuestions
from .llm import LLMClient
from .paths import PROJECT_ROOT
from .prompts import load_prompt
from .schemas import JDDraftLLM, utc_now
from .storage import write_json_atomic

PROMPT_DIR = "jd_writer"
MAX_BRIEF_CHARS = 6000


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


def draft_jd(config: AppConfig, llm: LLMClient, brief: str, *, company_name: str | None = None) -> dict:
    brief = (brief or "").strip()
    if len(brief) < 20:
        raise JDError("describe the role in at least a sentence or two (what the person will do, key skills)")
    if len(brief) > MAX_BRIEF_CHARS:
        raise JDError(f"the brief is too long ({len(brief)} characters, max {MAX_BRIEF_CHARS})")
    company = (company_name or "").strip() or _existing_company(config) or "our company"
    system = load_prompt(PROMPT_DIR, "system.md")
    template = load_prompt(PROMPT_DIR, "draft_jd.md")
    out = llm.generate_json(system=system.text, prompt=template.render(
        brief=brief, company_name=company, today=date.today().isoformat()), schema=JDDraftLLM)

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
    draft = {"created_at": utc_now(), "brief": brief, "llm": f"{llm.provider}/{llm.model}",
             "job": job, "questions": [q.model_dump() for q in questions], "language_notes": out.language_notes}
    write_json_atomic(_draft_path(config), draft)
    return draft


def load_draft(config: AppConfig) -> dict | None:
    p = _draft_path(config)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError):
        return None


def _yaml(header: str, data: dict) -> str:
    return header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def approve_jd(config: AppConfig, job: dict, questions: list[dict], *, by: str,
               scored_candidates: int = 0) -> dict:
    """Validate the (edited) draft and make it the live JD + questions."""
    by = (by or "").strip()
    if not by or len(by) > 80:
        raise JDError("say who is approving (a name, up to 80 characters)")
    try:
        jd = JobDescription.model_validate({**job, "approved_by": by, "approved_at": utc_now()})
        qs = ScreeningQuestions.model_validate({"questions": questions})
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:6])
        raise JDError(f"the draft isn't valid yet: {errs}") from None
    if jd.min_experience_years is not None and jd.max_experience_years is not None \
            and jd.min_experience_years > jd.max_experience_years:
        raise JDError("minimum experience is above the maximum")

    jd_path = PROJECT_ROOT / config.settings.files.job_description
    q_path = PROJECT_ROOT / config.settings.files.screening_questions
    history = jd_path.parent / "history"
    history.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    for p in (jd_path, q_path):
        if p.exists():
            shutil.copy2(p, history / f"{stamp}_{p.name}")

    jd_path.write_text(_yaml(f"# Approved by {by} on {jd.approved_at} (drafted with the JD writer).\n"
                             "# The role candidates are scored against. Previous versions: config/history/\n",
                             jd.model_dump(exclude_none=True)), encoding="utf-8")
    q_path.write_text(_yaml("# Questions the screening agent asks. kind: role = about the job (scored in Stage 4).\n"
                            "# `id` values are stable keys used in stage3 records -> screening.answers[].question_id\n",
                            {"questions": [q.model_dump() for q in qs.questions]}), encoding="utf-8")
    config.reload_files()
    _draft_path(config).unlink(missing_ok=True)
    return {"job_id": jd.job_id, "title": jd.title, "questions": len(qs.questions),
            "backup": f"{history.name}/{stamp}_*", "already_scored": scored_candidates}
