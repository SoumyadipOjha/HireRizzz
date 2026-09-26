"""Resume credibility: does the resume hang together, and does it match the candidate's LinkedIn?

Two kinds of evidence, both flagged, never used to reject anyone automatically:

1. Resume checks (code only, no LLM), from the Stage 1 profile (+ the resume text):
   overlapping full-time jobs, claimed experience higher than the job dates add
   up to, work long before graduating, "N years of X" older than X itself,
   impossible dates, roles without dates.
2. LinkedIn cross-check: the candidate's LinkedIn "Save to PDF" export is read
   with the same extraction prompt as resumes (one LLM call), then compared job
   by job IN CODE: companies missing on either side, start/end dates that
   disagree, different titles, a different name.

LinkedIn is self-reported too: a match raises confidence, it is not proof of
employment. Real proof (EPFO/UAN, background checks) belongs after the final list.
"""

from __future__ import annotations

import re
from datetime import date

from .schemas import (
    CredibilityFlag,
    CredibilityRecord,
    CredibilitySummary,
    LLMInfo,
    ResumeExtractionLLM,
    WorkExperience,
    utc_now,
)

# First public release year of technologies people commonly claim "N years of".
TECH_RELEASED = {
    "fastapi": 2018, "kubernetes": 2014, "docker": 2013, "react": 2013, "typescript": 2012, "rust": 2015,
    "airflow": 2015, "next.js": 2016, "nextjs": 2016, "vue": 2014, "angular": 2016, "flutter": 2017,
    "kotlin": 2016, "swift": 2014, "go": 2012, "golang": 2012, "terraform": 2014, "pytorch": 2016,
    "tensorflow": 2015, "langchain": 2022, "chatgpt": 2022, "openai": 2020, "llm": 2020, "gpt": 2020,
    "snowflake": 2014, "databricks": 2013, "dbt": 2016, "graphql": 2015, "svelte": 2016, "deno": 2020,
    "bun": 2022, "tailwind": 2017, "microsoft fabric": 2023, "power bi": 2015, "polars": 2020,
    "pydantic": 2017, "streamlit": 2019, "github actions": 2019, "spark": 2014, "kafka": 2011,
    "elasticsearch": 2010, "mongodb": 2009, "node.js": 2009, "nodejs": 2009, "redis": 2009,
}

_COMPANY_NOISE = re.compile(
    r"\b(pvt|private|ltd|limited|llc|llp|inc|incorporated|corp|corporation|co|company|technologies|technology|"
    r"tech|solutions|services|software|systems|labs|group|global|india|the)\b")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                       "nov", "dec"], 1)}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def month_index(value: str | None) -> int | None:
    """'2022-04' / '2022' / 'Apr 2022' -> months since year 0 (year-only = January)."""
    if not value:
        return None
    v = value.strip().lower()
    m = re.match(r"^(\d{4})(?:-(\d{1,2}))?$", v)
    if m:
        return int(m.group(1)) * 12 + (int(m.group(2) or 1) - 1)
    m = re.match(r"^([a-z]{3})[a-z]*\.?\s+(\d{4})$", v)
    if m and m.group(1) in _MONTHS:
        return int(m.group(2)) * 12 + _MONTHS[m.group(1)] - 1
    return None


def _today_index(today: date) -> int:
    return today.year * 12 + today.month - 1


def _fmt(idx: int | None) -> str:
    if idx is None:
        return "?"
    return f"{['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][idx % 12]} {idx // 12}"


def span(w: WorkExperience, today: date) -> tuple[int | None, int | None]:
    start = month_index(w.start_date)
    end = _today_index(today) if (w.is_current and not w.end_date) else month_index(w.end_date)
    return start, end


def _role(w: WorkExperience, today: date) -> str:
    s, e = span(w, today)
    return f"{w.title or '?'} at {w.company or '?'} ({_fmt(s)} – {'Present' if w.is_current else _fmt(e)})"


def _is_internship(w: WorkExperience) -> bool:
    return bool(re.search(r"\b(intern|internship|trainee|apprentice|student|freelance|part[- ]time)\b",
                          f"{w.title or ''} {w.company or ''}", re.I))


def norm_company(name: str | None) -> str:
    n = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    return " ".join(_COMPANY_NOISE.sub(" ", n).split())


def _words(text: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 1}


def same_company(a: str | None, b: str | None) -> bool:
    na, nb = norm_company(a), norm_company(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    wa, wb = set(na.split()), set(nb.split())
    return len(wa & wb) / max(1, min(len(wa), len(wb))) >= 0.6


def _months_covered(spans: list[tuple[int, int]]) -> int:
    """Union length of [start, end) month ranges, so overlapping jobs aren't double-counted."""
    total, cur_s, cur_e = 0, None, None
    for s, e in sorted(spans):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


# ---------------------------------------------------------------------------
# 1. resume checks
# ---------------------------------------------------------------------------

def resume_checks(profile: ResumeExtractionLLM, resume_text: str | None = None,
                  today: date | None = None) -> list[CredibilityFlag]:
    today = today or date.today()
    now = _today_index(today)
    flags: list[CredibilityFlag] = []
    jobs = profile.work_experience
    full_time = [w for w in jobs if not _is_internship(w)]

    for w in jobs:
        s, e = span(w, today)
        if s is None:
            flags.append(CredibilityFlag(level="amber", check="missing_dates",
                                         message=f"No start date for {w.title or 'a role'} at {w.company or '?'}.",
                                         resume=_role(w, today)))
            continue
        if s > now + 1:
            flags.append(CredibilityFlag(level="red", check="future_date",
                                         message="A role starts in the future.", resume=_role(w, today)))
        if e is not None and e < s:
            flags.append(CredibilityFlag(level="red", check="end_before_start",
                                         message="A role ends before it starts.", resume=_role(w, today)))

    # overlapping full-time roles (more than 2 months)
    dated = [(w, *span(w, today)) for w in full_time]
    dated = [(w, s, e) for w, s, e in dated if s is not None and e is not None and e >= s]
    for i in range(len(dated)):
        for j in range(i + 1, len(dated)):
            (a, as_, ae), (b, bs, be) = dated[i], dated[j]
            overlap = min(ae, be) - max(as_, bs)
            if overlap > 2:
                flags.append(CredibilityFlag(
                    level="amber", check="timeline_overlap",
                    message=f"Two full-time roles overlap by {overlap} months.",
                    resume=f"{_role(a, today)}  |  {_role(b, today)}"))

    # claimed total experience vs what the dates add up to
    covered = _months_covered([(s, e) for _, s, e in dated]) / 12
    claimed = profile.total_experience_years
    if claimed is not None and dated and claimed - covered > 1.0:
        flags.append(CredibilityFlag(
            level="red" if claimed - covered > 2.5 else "amber", check="experience_inflated",
            message=f"Claims {claimed:g} years of experience, but the dated full-time roles add up to "
                    f"{covered:.1f} years.",
            resume=f"total_experience_years = {claimed:g}; roles: " + "; ".join(_role(w, today) for w, _, _ in dated)))

    # full-time work long before graduating
    grads = [e.graduation_year for e in profile.education if e.graduation_year]
    if grads:
        first_grad = min(grads)
        for w, s, _ in dated:
            if s < (first_grad - 1) * 12:
                flags.append(CredibilityFlag(
                    level="amber", check="work_before_graduation",
                    message=f"A full-time role starts more than a year before graduating ({first_grad}).",
                    resume=_role(w, today)))

    # "5 years of FastAPI" when FastAPI is younger than that
    for yrs, tech in _years_of_claims(resume_text or ""):
        released = TECH_RELEASED.get(tech)
        if released and yrs > (today.year - released) + 0.5:
            flags.append(CredibilityFlag(
                level="red", check="skill_older_than_tech",
                message=f"Claims {yrs:g} years of {tech}, but {tech} first came out in {released} "
                        f"(at most {today.year - released} years ago).",
                resume=f"“{yrs:g} years … {tech}”"))
    return _dedupe(flags)


# The skill name is read in a lookahead so it isn't consumed: in "5 years of LangChain and 6 years of
# Fabric" the second claim must still be found.
_YEARS_OF = re.compile(r"(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:experience\s+)?(?:in|with|of)?\s*"
                       r"(?=([a-z][a-z0-9.+# ]{0,24}))", re.I)


def _years_of_claims(text: str) -> list[tuple[float, str]]:
    out = []
    for m in _YEARS_OF.finditer(text):
        tail = m.group(2).lower()
        for tech in sorted(TECH_RELEASED, key=len, reverse=True):  # longest name first ("next.js" before "next")
            if re.match(rf"{re.escape(tech)}\b", tail):
                out.append((float(m.group(1)), tech))
                break
    return out


def _dedupe(flags: list[CredibilityFlag]) -> list[CredibilityFlag]:
    seen, out = set(), []
    for f in flags:
        key = (f.check, f.message, f.resume)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


# ---------------------------------------------------------------------------
# 2. LinkedIn cross-check
# ---------------------------------------------------------------------------

DATE_TOLERANCE_MONTHS = 3


def linkedin_checks(resume: ResumeExtractionLLM, linkedin: ResumeExtractionLLM,
                    today: date | None = None) -> list[CredibilityFlag]:
    today = today or date.today()
    flags: list[CredibilityFlag] = []

    rn, ln = _words(resume.full_name), _words(linkedin.full_name)
    if rn and ln and not (rn & ln):
        flags.append(CredibilityFlag(level="red", check="linkedin_name",
                                     message="The LinkedIn profile is for a different name. Is it the right profile?",
                                     resume=resume.full_name, linkedin=linkedin.full_name))

    unmatched_li = list(linkedin.work_experience)
    for w in resume.work_experience:
        match = next((l for l in unmatched_li if same_company(w.company, l.company)), None)
        if match is None:
            flags.append(CredibilityFlag(
                level="amber" if _is_internship(w) else "red", check="linkedin_missing_company",
                message=f"{w.company or 'A company'} is on the resume but not on LinkedIn.",
                resume=_role(w, today), linkedin="(not listed)"))
            continue
        unmatched_li.remove(match)
        problems = []
        rs, re_ = span(w, today)
        ls, le = span(match, today)
        for label, a, b in (("start", rs, ls), ("end", re_, le)):
            if a is not None and b is not None and abs(a - b) > DATE_TOLERANCE_MONTHS:
                problems.append((label, abs(a - b)))
        if w.is_current != match.is_current and not (w.is_current and le is not None and _today_index(today) - le <= 2):
            problems.append(("current", 0))
        if problems:
            worst = max((d for _, d in problems), default=0)
            what = ", ".join(("current role" if p == "current" else f"{p} date ({d} months apart)")
                             for p, d in problems)
            flags.append(CredibilityFlag(
                level="red" if worst > 12 or any(p == "current" for p, _ in problems) else "amber",
                check="linkedin_dates", message=f"Dates for {w.company} don't match LinkedIn: {what}.",
                resume=_role(w, today), linkedin=_role(match, today)))
        elif w.title and match.title and not (_words(w.title) & _words(match.title)):
            flags.append(CredibilityFlag(level="amber", check="linkedin_title",
                                         message=f"Different job title at {w.company} on LinkedIn.",
                                         resume=_role(w, today), linkedin=_role(match, today)))
        else:
            flags.append(CredibilityFlag(level="green", check="linkedin_match",
                                         message=f"{w.company}: title and dates match LinkedIn.",
                                         resume=_role(w, today), linkedin=_role(match, today)))
    for l in unmatched_li:
        flags.append(CredibilityFlag(level="amber", check="linkedin_extra_company",
                                     message=f"{l.company or 'A company'} is on LinkedIn but not on the resume.",
                                     resume="(not listed)", linkedin=_role(l, today)))

    r_years, l_years = resume.total_experience_years, linkedin.total_experience_years
    if r_years is not None and l_years is not None and r_years - l_years > 1.0:
        flags.append(CredibilityFlag(level="amber", check="linkedin_total_experience",
                                     message=f"The resume claims {r_years:g} years; LinkedIn adds up to {l_years:g}.",
                                     resume=f"{r_years:g} years", linkedin=f"{l_years:g} years"))
    return flags


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------

def summarize(flags: list[CredibilityFlag], linkedin: bool) -> CredibilitySummary:
    return CredibilitySummary(red=sum(f.level == "red" for f in flags), amber=sum(f.level == "amber" for f in flags),
                              green=sum(f.level == "green" for f in flags), linkedin=linkedin, updated_at=utc_now())


def _resume_text(config, entry) -> str | None:
    from .paths import resolve_stored
    from .resume_reader import read_resume

    try:
        return read_resume(resolve_stored(entry.source_file))
    except Exception:  # file gone (e.g. ephemeral disk on a host): checks that need text are skipped
        return None


def build_record(config, entry, *, linkedin_profile: ResumeExtractionLLM | None = None,
                 linkedin_file: str | None = None, llm_info: LLMInfo | None = None,
                 today: date | None = None) -> CredibilityRecord:
    from .stage2_shortlist import load_stage1

    resume = load_stage1(entry, config.store).extraction
    flags = resume_checks(resume, _resume_text(config, entry), today)
    if config.settings.credibility.company_check:
        from .company_check import company_checks, default_lookup

        flags += company_checks(resume, default_lookup(), today)
    if linkedin_profile is not None:
        flags += linkedin_checks(resume, linkedin_profile, today)
    order = {"red": 0, "amber": 1, "green": 2}
    flags.sort(key=lambda f: order[f.level])
    return CredibilityRecord(candidate_id=entry.candidate_id, flags=flags,
                             summary=summarize(flags, linkedin_profile is not None), linkedin_file=linkedin_file,
                             linkedin_profile=linkedin_profile, llm=llm_info)


def save_record(ctx, record: CredibilityRecord) -> CredibilityRecord:
    """Store the result; enough red issues stop the candidate (and cancel an open interview link)."""
    cc = ctx.config.settings.credibility
    ctx.config.store.put_record("credibility", record.candidate_id, record.model_dump(mode="json"))
    was = ctx.index.get(record.candidate_id).fraud_blocked
    entry = ctx.index.set_credibility(record.candidate_id, record.summary,
                                      block_min_red=cc.min_red_to_block if cc.block_on_fraud else 0)
    if entry.fraud_blocked and not was:
        ctx.logger.warning("credibility: candidate_id=%s FRAUD DETECTED (%d red issues): stopped before further "
                           "stages", record.candidate_id, record.summary.red)
        from .agent.invites import InviteStore

        invites = InviteStore(ctx.config.store)
        if inv := invites.active_for(record.candidate_id):
            invites.update(inv.token, status="revoked")
            ctx.logger.info("credibility: candidate_id=%s interview link revoked", record.candidate_id)
        if cc.email_candidate_on_fraud == "auto":
            try:
                request_clarification(ctx, record.candidate_id, record=record)
            except Exception as e:  # an email problem never undoes the block
                ctx.logger.error("credibility: candidate_id=%s clarification email not sent: %s", record.candidate_id, e)
    return record


def candidate_wording(flag: CredibilityFlag) -> str:
    """One mismatch, phrased for the candidate (no internal labels, never the word 'fraud')."""
    msg = flag.message.replace("The resume claims ", "Your resume mentions ", 1)
    if msg.startswith("Claims "):
        msg = "Your resume mentions " + msg[len("Claims "):]
    msg = msg.replace(" is on the resume but not on LinkedIn.", " is on your resume but not on your LinkedIn profile.")
    lines = [f"- {msg}"]
    if flag.resume and flag.resume != "(not listed)" and flag.linkedin:
        lines.append(f"   Resume: {flag.resume}")
    if flag.linkedin and flag.linkedin != "(not listed)":
        lines.append(f"   LinkedIn: {flag.linkedin}")
    return "\n".join(lines)


def request_clarification(ctx, candidate_id: str, *, record: CredibilityRecord | None = None, mailer=None):
    """Email the candidate the red mismatches and ask for an updated resume or an explanation."""
    from .agent.notify import candidate_contact, first_name
    from .config import ConfigError
    from .mailer import EmailError, make_mailer, render_email, valid_email
    from .schemas import Notification

    record = record or load_record(ctx.config, candidate_id)
    entry = ctx.index.get(candidate_id)
    reds = [f for f in (record.flags if record else []) if f.level == "red"]
    if not reds:
        raise ValueError("there are no mismatches to ask the candidate about")
    try:
        name, address = candidate_contact(ctx.config, entry)
    except Exception:
        name, address = entry.display_name, None
    job = ctx.config.job
    if not valid_email(address):
        n = Notification(kind="clarification", status="skipped", at=utc_now(), error="no valid email address in the resume")
    else:
        try:
            mailer = mailer or make_mailer(ctx.config)
            mailer.send(render_email("clarification", to=address,
                                     subject=f"Your application for {job.title}: a few details to confirm",
                                     values={"first_name": first_name(name), "job_title": job.title,
                                             "company_name": job.company_name,
                                             "items": "\n\n".join(candidate_wording(f) for f in reds)}))
            n = Notification(kind="clarification", status="outbox" if mailer.mode == "outbox" else "sent",
                             to=address, at=utc_now())
        except (EmailError, ConfigError) as e:
            n = Notification(kind="clarification", status="failed", to=address, at=utc_now(), error=str(e)[:500])
    ctx.index.set_notification(candidate_id, "credibility", n)
    (ctx.logger.error if n.status == "failed" else ctx.logger.info)(
        "credibility: candidate_id=%s clarification email %s%s", candidate_id, n.status, f" ({n.error})" if n.error else "")
    return n


def clear_fraud(ctx, candidate_id: str, *, by: str, note: str | None = None):
    """A recruiter reviewed the flags and lets the candidate continue. Recorded; undone if new issues appear."""
    from .schemas import FraudClearance

    by = (by or "").strip()
    if not by or len(by) > 80:
        raise ValueError("say who is clearing the flag (a name, up to 80 characters)")
    entry = ctx.index.get(candidate_id)
    if not entry.fraud_blocked:
        raise ValueError("this candidate isn't blocked")
    red = entry.credibility.red if entry.credibility else 0
    ctx.logger.info("credibility: candidate_id=%s fraud flag CLEARED by %s (%d red issues)%s", candidate_id, by, red,
                    f": {note}" if note else "")
    return ctx.index.clear_fraud(candidate_id, FraudClearance(by=by, at=utc_now(), note=(note or None) and note[:300],
                                                               red_at_clearance=red))


def load_record(config, candidate_id: str) -> CredibilityRecord | None:
    doc = config.store.get_record(config.store.ref("credibility", candidate_id))
    return CredibilityRecord.model_validate(doc) if doc else None


def check_resume(ctx, candidate_id: str) -> CredibilityRecord:
    """(Re)run the resume checks, keeping an earlier LinkedIn comparison if there was one."""
    entry = ctx.index.get(candidate_id)
    prev = load_record(ctx.config, candidate_id)
    li = prev.linkedin_profile if prev else None
    return save_record(ctx, build_record(ctx.config, entry, linkedin_profile=li,
                                         linkedin_file=prev.linkedin_file if prev else None,
                                         llm_info=prev.llm if prev else None))


def check_linkedin(ctx, llm, candidate_id: str, pdf_path, file_name: str) -> CredibilityRecord:
    """Read the LinkedIn PDF (one LLM call, same prompt as resumes) and compare it with the resume."""
    from .prompts import load_prompt
    from .resume_reader import ResumeReadError, read_resume

    text = read_resume(pdf_path)
    if len(text) < 150:
        raise ResumeReadError("the LinkedIn PDF has almost no text. Use LinkedIn's own 'Save to PDF', "
                              "not a screenshot or a scan")
    system = load_prompt("stage1_extraction", "system.md")
    template = load_prompt("stage1_extraction", "extract_resume.md")
    profile = llm.generate_json(system=system.text,
                                prompt=template.render(resume_text=text[:60000], today=date.today().isoformat()),
                                schema=ResumeExtractionLLM)
    entry = ctx.index.get(candidate_id)
    record = build_record(ctx.config, entry, linkedin_profile=profile, linkedin_file=file_name,
                          llm_info=LLMInfo(provider=llm.provider, model=llm.model, prompt_file=template.rel_path))
    ctx.logger.info("credibility: candidate_id=%s LinkedIn compared: %d red, %d amber, %d green", candidate_id,
                    record.summary.red, record.summary.amber, record.summary.green)
    return save_record(ctx, record)
