"""Stage 4 — score the screening interview and suggest a final decision.

The LLM scores four competencies from the transcript and must back each score
with quotes of the candidate's own words. Code then:
* keeps only quotes that really appear in the candidate's lines (anything else
  is recorded as `unverified_quotes`, never shown as evidence),
* applies the competency weights -> interview_score,
* combines it with the Stage 2 resume score -> final_score,
* suggests shortlisted / rejected against `evaluation.final_threshold`,
* flags `needs_review` when evidence is thin or there are red flags.

The suggestion is not the decision: the hiring manager approves the final
lists (see approvals). Same log-and-skip policy as every other stage.
"""

from __future__ import annotations

import json
import re

from .context import RunContext, StageSummary
from .llm import LLMClient
from .prompts import load_prompt
from .schemas import (
    COMPETENCIES,
    CompetencyResult,
    InterviewEvaluationLLM,
    LLMInfo,
    Stage3Record,
    Stage4Record,
)
from .stage2_shortlist import JoinKeyMismatchError, load_stage1
from .stage3_call import load_stage2
from .storage import Store, load_model

STAGE = "stage4_evaluation"
UPSTREAM = "stage3_calling"
PROMPT_DIR = "stage4_evaluation"

_WS = re.compile(r"\s+")
_EDGE = re.compile(r"^[\s\"'“”‘’.,;:!?…-]+|[\s\"'“”‘’.,;:!?…-]+$")


def _norm(text: str) -> str:
    return _WS.sub(" ", text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')).strip().lower()


def candidate_speech(transcript: str) -> str:
    """Everything the candidate said, normalised, one line per turn."""
    lines = [ln[len("Candidate:"):] for ln in transcript.splitlines() if ln.startswith("Candidate:")]
    return "\n".join(_norm(ln) for ln in lines)


def verify_quotes(quotes: list[str], speech: str) -> tuple[list[str], list[str]]:
    """(verified, unverified). A quote is verified if it appears verbatim (ignoring case,
    spacing and surrounding punctuation) inside one of the candidate's turns."""
    ok, bad = [], []
    turns = speech.splitlines()
    for q in quotes:
        core = _norm(_EDGE.sub("", q or ""))
        if len(core) >= 3 and any(core in t for t in turns):
            ok.append(q.strip())
        elif q and q.strip():
            bad.append(q.strip())
    return ok, bad


def load_stage3(entry, store: Store) -> Stage3Record:
    st = entry.stages[UPSTREAM]
    record = load_model(store, st.output_path, Stage3Record)
    if record.candidate_id != entry.candidate_id:
        raise JoinKeyMismatchError(f"{st.output_path} has candidate_id {record.candidate_id}")
    return record


def _resume_summary(s1) -> str:
    ex = s1.extraction
    return json.dumps({"headline": ex.headline, "total_experience_years": ex.total_experience_years,
                       "current_title": ex.current_title, "skills": ex.skills[:40], "summary": ex.summary},
                      ensure_ascii=False, indent=2)


def score(ctx: RunContext, assessment: InterviewEvaluationLLM, speech: str, resume_score: float,
          red_flags: list[str]) -> dict:
    """Deterministic part of Stage 4 (weights, verification, decision). Returns Stage4Record fields."""
    ev = ctx.config.settings.evaluation
    competencies, review = [], []
    for name in COMPETENCIES:
        a = getattr(assessment, name)
        w = ev.competency_weights[name]
        ok, bad = verify_quotes(a.evidence_quotes, speech)
        competencies.append(CompetencyResult(competency=name, score=a.score, weight=w,
                                             weighted_score=round(a.score * w, 2), evidence_quotes=ok,
                                             unverified_quotes=bad, rationale=a.rationale))
        if a.score > 0 and len(ok) < ev.min_verified_quotes:
            review.append(f"{name}: score {a.score} has no verified quote from the candidate")
        if bad:
            review.append(f"{name}: {len(bad)} quote(s) not found in the transcript were dropped")
    interview = round(sum(c.weighted_score for c in competencies), 2)
    final = round(resume_score * ev.resume_weight + interview * ev.interview_weight, 2)
    decision = "shortlisted" if final >= ev.final_threshold else "rejected"
    reasons = [f"final_score {final:g} {'≥' if decision == 'shortlisted' else '<'} threshold {ev.final_threshold:g} "
               f"(resume {resume_score:g} × {ev.resume_weight:g} + interview {interview:g} × {ev.interview_weight:g})"]
    if red_flags:
        review.append(f"call red flags: {'; '.join(red_flags)[:300]}")
    return dict(competencies=competencies, interview_score=interview, resume_score=resume_score,
                resume_weight=ev.resume_weight, interview_weight=ev.interview_weight, final_score=final,
                threshold=ev.final_threshold, suggested_decision=decision, decision_reasons=reasons,
                needs_review=bool(review), review_reasons=review)


def run_stage4(ctx: RunContext, llm: LLMClient, *, force: bool = False,
               only: set[str] | None = None) -> StageSummary:
    summary = StageSummary(STAGE)
    system = load_prompt(PROMPT_DIR, "system.md")
    template = load_prompt(PROMPT_DIR, "evaluate_interview.md")
    job = ctx.config.job
    store = ctx.config.store
    log = ctx.logger
    questions = "\n".join(f"{i}. {q.question}" for i, q in enumerate(ctx.config.questions.questions, 1))

    for entry in ctx.index.all():
        cid = entry.candidate_id
        if only and cid not in only:
            continue
        st = entry.stages[STAGE]
        if st.status == "success" and not force:
            summary.already_done.append(cid)
            continue
        up = entry.stages[UPSTREAM]
        if up.status != "success":
            # Not an error: most candidates are simply not there yet (or were rejected earlier).
            if st.status != "pending":
                ctx.skip(STAGE, entry, f"{UPSTREAM} is '{up.status}', not 'success'")
            summary.skipped.append(cid)
            continue

        log.info("%s: candidate_id=%s (%s)", STAGE, cid, entry.display_name)
        try:
            s1 = load_stage1(entry, store)
            s2 = load_stage2(entry, store)
            s3 = load_stage3(entry, store)
            transcript = store.get_text(s3.transcript_path)
            if not transcript or not transcript.strip():
                raise ValueError(f"transcript {s3.transcript_path} is missing or empty")
            prompt = template.render(job_title=job.title, must_have_skills=", ".join(job.must_have_skills),
                                     job_description=job.description.strip(), resume_summary=_resume_summary(s1),
                                     questions=questions, transcript=transcript.strip())
            assessment = llm.generate_json(system=system.text, prompt=prompt, schema=InterviewEvaluationLLM)
            fields = score(ctx, assessment, candidate_speech(transcript), s2.overall_score,
                           s3.screening.red_flags)
            record = Stage4Record(candidate_id=cid, run_id=ctx.run_id,
                                  llm=LLMInfo(provider=llm.provider, model=llm.model, prompt_file=template.rel_path),
                                  job_id=job.job_id, concerns=assessment.concerns, summary=assessment.summary,
                                  **fields)
            ref = store.put_record("stage4_evaluation", cid, record.model_dump(mode="json"))
            ctx.index.set_stage(cid, STAGE, "success", run_id=ctx.run_id, output_path=ref,
                                decision=record.suggested_decision, score=record.final_score,
                                note="needs review: " + "; ".join(record.review_reasons)[:400]
                                if record.needs_review else None)
            log.info("%s: candidate_id=%s suggested %s final=%s (interview %s, resume %s)%s", STAGE, cid,
                     record.suggested_decision.upper(), record.final_score, record.interview_score,
                     record.resume_score, " NEEDS REVIEW" if record.needs_review else "")
            summary.succeeded.append(cid)
        except Exception as e:
            ctx.fail(STAGE, entry, e)
            summary.failed.append(cid)

    log.info(summary.line())
    return summary
