"""Stage 2 — score each extracted profile against the job; code makes the decision."""

from __future__ import annotations

import json

from .config import CRITERIA, JobDescription, Settings
from .context import RunContext, StageSummary
from .llm import LLMClient
from .prompts import load_prompt
from .storage import Store, load_model
from .schemas import CriterionResult, LLMInfo, ResumeExtractionLLM, ShortlistAssessmentLLM, Stage1Record, Stage2Record

FRAUD_NOTE = "blocked: fraud detected by the resume credibility checks (a recruiter can clear it)"
STAGE = "stage2_shortlisting"
UPSTREAM = "stage1_extraction"
PROMPT_DIR = "stage2_shortlisting"

# Identity/contact fields are withheld from the scorer so they cannot influence scores.
BLIND_FIELDS = {"full_name", "email", "phone", "linkedin_url", "location"}


class JoinKeyMismatchError(Exception):
    """A stage file's candidate_id does not match the index entry."""


def blind_profile(extraction: ResumeExtractionLLM) -> str:
    return json.dumps(extraction.model_dump(mode="json", exclude=BLIND_FIELDS), indent=2, ensure_ascii=False)


def _norm(skill: str) -> str:
    return " ".join(skill.lower().replace("-", " ").split())


def reconcile_skills(llm_matched: list[str], llm_missing: list[str], jd_skills: list[str],
                     warn) -> tuple[list[str], list[str]]:
    """Map LLM skill names back onto the JD's wording. Every JD skill ends up in exactly
    one of (matched, missing); anything the LLM did not clearly match counts as missing."""
    canon = {_norm(s): s for s in jd_skills}
    matched_norm = {_norm(s) for s in llm_matched}
    missing_norm = {_norm(s) for s in llm_missing}
    unknown = (matched_norm | missing_norm) - canon.keys()
    if unknown:
        warn(f"LLM returned skills not in the JD (ignored): {sorted(unknown)}")
    both = matched_norm & missing_norm & canon.keys()
    if both:
        warn(f"LLM listed skills as both matched and missing (treated as missing): {sorted(canon[n] for n in both)}")
    matched = [s for s in jd_skills if _norm(s) in matched_norm and _norm(s) not in missing_norm]
    missing = [s for s in jd_skills if s not in matched]
    return matched, missing


def decide(assessment: ShortlistAssessmentLLM, settings: Settings, missing_must: list[str]):
    weights = settings.scoring.weights
    criteria = []
    for name in CRITERIA:
        a = getattr(assessment, name)
        criteria.append(CriterionResult(criterion=name, score=a.score, weight=weights[name],
                                        weighted_score=round(a.score * weights[name], 2), evidence=a.evidence))
    overall = round(sum(c.score * c.weight for c in criteria), 2)

    th = settings.thresholds
    reasons = []
    passed_score = overall >= th.shortlist_score
    reasons.append(f"overall_score {overall} {'>=' if passed_score else '<'} threshold {th.shortlist_score:g}")
    passed_must = True
    if th.require_all_must_have:
        passed_must = not missing_must
        reasons.append("all must-have skills evidenced" if passed_must
                       else f"missing must-have skills (require_all_must_have=true): {', '.join(missing_must)}")
    decision = "shortlisted" if passed_score and passed_must else "rejected"
    return criteria, overall, decision, reasons


def load_stage1(entry, store: Store) -> Stage1Record:
    st = entry.stages[UPSTREAM]
    record = load_model(store, st.output_path, Stage1Record)
    if record.candidate_id != entry.candidate_id:
        raise JoinKeyMismatchError(f"{st.output_path} has candidate_id {record.candidate_id}")
    return record


def _job_fields(job: JobDescription) -> dict:
    exp_range = (f"{job.min_experience_years:g}-{job.max_experience_years:g} years"
                 if job.min_experience_years is not None and job.max_experience_years is not None
                 else f"at least {job.min_experience_years:g} years" if job.min_experience_years is not None
                 else "not specified")
    return dict(
        job_id=job.job_id, job_title=job.title, job_location=job.location or "not specified",
        must_have_skills=", ".join(job.must_have_skills),
        nice_to_have_skills=", ".join(job.nice_to_have_skills) or "none",
        job_description=job.description.strip(), experience_range=exp_range,
    )


def run_stage2(ctx: RunContext, llm: LLMClient, *, force: bool = False,
               only: set[str] | None = None) -> StageSummary:
    summary = StageSummary(STAGE)
    system = load_prompt(PROMPT_DIR, "system.md")
    template = load_prompt(PROMPT_DIR, "score_candidate.md")
    settings = ctx.config.settings
    log = ctx.logger
    jobs: dict[str, tuple[JobDescription, dict]] = {}   # each candidate is scored against the job they applied to

    for entry in ctx.index.all():
        cid = entry.candidate_id
        if only and cid not in only:
            continue
        st = entry.stages[STAGE]
        if st.status == "success" and not force:
            summary.already_done.append(cid)
            continue
        if entry.fraud_blocked:  # stopped by the credibility checks
            if entry.stages[STAGE].note != FRAUD_NOTE:
                ctx.skip(STAGE, entry, FRAUD_NOTE)
            summary.skipped.append(cid)
            continue
        upstream = entry.stages[UPSTREAM].status
        if upstream != "success":
            ctx.skip(STAGE, entry, f"{UPSTREAM} is '{upstream}', not 'success'")
            summary.skipped.append(cid)
            continue

        log.info("%s: candidate_id=%s (%s)", STAGE, cid, entry.display_name)
        try:
            jid = ctx.config.jobs.candidate_job(entry)
            if jid not in jobs:
                jd = ctx.config.for_job(jid).job
                jobs[jid] = (jd, _job_fields(jd))
            job, job_fields = jobs[jid]
            s1 = load_stage1(entry, ctx.config.store)
            prompt = template.render(candidate_profile=blind_profile(s1.extraction), **job_fields)
            assessment = llm.generate_json(system=system.text, prompt=prompt, schema=ShortlistAssessmentLLM)

            warn = lambda m: log.warning("%s: candidate_id=%s %s", STAGE, cid, m)  # noqa: E731
            matched_must, missing_must = reconcile_skills(
                assessment.matched_must_have_skills, assessment.missing_must_have_skills, job.must_have_skills, warn)
            matched_nice, _ = reconcile_skills(assessment.matched_nice_to_have_skills, [], job.nice_to_have_skills,
                                               warn)
            criteria, overall, decision, reasons = decide(assessment, settings, missing_must)

            record = Stage2Record(
                candidate_id=cid, run_id=ctx.run_id,
                llm=LLMInfo(provider=llm.provider, model=llm.model, prompt_file=template.rel_path),
                job_id=job.job_id, criteria=criteria,
                matched_must_have_skills=matched_must, missing_must_have_skills=missing_must,
                matched_nice_to_have_skills=matched_nice,
                overall_score=overall, threshold=settings.thresholds.shortlist_score,
                require_all_must_have=settings.thresholds.require_all_must_have,
                decision=decision, decision_reasons=reasons, rationale=assessment.rationale,
            )
            ref = ctx.config.store.put_record("stage2_shortlist", cid, record.model_dump(mode="json"))
            ctx.index.set_stage(cid, STAGE, "success", run_id=ctx.run_id, output_path=ref,
                                decision=decision, score=overall)
            log.info("%s: candidate_id=%s %s score=%s (%s)", STAGE, cid, decision.upper(), overall, "; ".join(reasons))
            summary.succeeded.append(cid)
        except Exception as e:
            ctx.fail(STAGE, entry, e)
            summary.failed.append(cid)

    log.info(summary.line())
    return summary
