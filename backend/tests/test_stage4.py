"""Stage 4: interview evaluation (quote verification, scoring maths, suggested decision)."""

from __future__ import annotations

import pytest
from conftest import INVENTED_QUOTE, FakeLLM, interview_evaluation, turn

from screening.agent.dialogue import ScreeningDialogue
from screening.config import EvaluationConfig
from screening.index import CandidateIndex
from screening.schemas import CandidateEntry, InterviewEvaluationLLM, Stage4Record
from screening.stage0_ingest import ingest
from screening.stage1_extract import run_stage1
from screening.stage2_shortlist import run_stage2
from screening.stage3_call import finalize_dialogue, run_stage3
from screening.stage4_evaluate import candidate_speech, run_stage4, verify_quotes

TRANSCRIPT = """Agent: Tell me about a recent project.
Candidate: I built an order API with FastAPI, and I owned the payments endpoints.
Agent: Great. A tricky bug?
Candidate: A slow query — I used EXPLAIN and added an index.
"""


def test_quotes_must_be_the_candidates_own_words():
    speech = candidate_speech(TRANSCRIPT)
    ok, bad = verify_quotes([
        "I built an order API with FastAPI",           # verbatim
        "  i USED explain and added an index.  ",      # case / spacing / punctuation don't matter
        "“I owned the payments endpoints”",  # curly quotes around it are fine
        "Tell me about a recent project",              # the agent said this, not the candidate
        "I built an order API with Django",            # changed wording
        "an",                                          # too short to count as evidence
    ], speech)
    assert ok == ["I built an order API with FastAPI", "i USED explain and added an index.",
                  "“I owned the payments endpoints”"]
    assert bad == ["Tell me about a recent project", "I built an order API with Django", "an"]


def _called(ctx, answers=None):
    """Aarav through Stages 0-3 with a completed call; returns his candidate_id."""
    ingest(ctx)
    run_stage1(ctx, FakeLLM())
    run_stage2(ctx, FakeLLM())
    run_stage3(ctx)
    aarav = next(e for e in ctx.index.all() if e.display_name == "Aarav Sharma")
    n = len(ctx.config.questions.questions)
    llm = FakeLLM(agent_script=[turn("consent_yes", True, "Great.")] + [turn("answer", True, "Thanks.")] * n)
    d = ScreeningDialogue(config=ctx.config, llm=llm, candidate_id=aarav.candidate_id, candidate_name="Aarav Sharma",
                          channel="browser_text")
    d.start()
    for a in answers or (["yes"] + [f"my answer number {i}" for i in range(n)]):
        if not d.ended:
            d.reply(a)
    assert finalize_dialogue(ctx, llm, d)
    return aarav.candidate_id


def test_scores_weights_and_suggestion(ctx):
    cid = _called(ctx)
    s = run_stage4(ctx, FakeLLM())
    assert s.succeeded == [cid] and s.failed == []
    rec = Stage4Record.model_validate(ctx.config.store.get_record(ctx.index.get(cid).stages["stage4_evaluation"]
                                                                  .output_path))
    by = {c.competency: c for c in rec.competencies}
    # FakeLLM scores 80/70/75/90 with the default weights .35/.25/.20/.20
    assert rec.interview_score == pytest.approx(80 * .35 + 70 * .25 + 75 * .20 + 90 * .20)
    resume = ctx.index.get(cid).stages["stage2_shortlisting"].score
    assert rec.resume_score == resume
    assert rec.final_score == pytest.approx(round(resume * .4 + rec.interview_score * .6, 2))
    assert rec.suggested_decision == "shortlisted" and rec.threshold == 65
    # the invented quote never becomes evidence, and it is flagged for review
    assert INVENTED_QUOTE not in by["communication"].evidence_quotes
    assert by["communication"].unverified_quotes == [INVENTED_QUOTE]
    assert rec.needs_review and any("not found in the transcript" in r for r in rec.review_reasons)
    assert by["role_knowledge"].evidence_quotes == ["my answer number 0"]

    entry = ctx.index.get(cid)
    st = entry.stages["stage4_evaluation"]
    assert st.decision == "shortlisted" and st.score == rec.final_score and entry.overall_status == "evaluated"
    assert st.note.startswith("needs review:")
    assert run_stage4(ctx, FakeLLM()).already_done == [cid]  # idempotent


def test_low_interview_suggests_rejection(ctx):
    cid = _called(ctx)

    class LowLLM(FakeLLM):
        def generate_json(self, *, system, prompt, schema, temperature=None):
            if schema is InterviewEvaluationLLM:
                return schema.model_validate(interview_evaluation(prompt, scores=(20, 10, 40, 30)))
            return super().generate_json(system=system, prompt=prompt, schema=schema, temperature=temperature)

    run_stage4(ctx, LowLLM())
    st = ctx.index.get(cid).stages["stage4_evaluation"]
    assert st.decision == "rejected" and st.score < 65


def test_candidates_without_a_finished_call_are_left_pending(ctx):
    ingest(ctx)
    run_stage1(ctx, FakeLLM())
    run_stage2(ctx, FakeLLM())
    run_stage3(ctx)
    s = run_stage4(ctx, FakeLLM())
    assert s.succeeded == [] and s.failed == [] and len(s.skipped) == 3
    assert all(e.stages["stage4_evaluation"].status == "pending" for e in ctx.index.all())


def test_llm_failure_is_log_and_skip(ctx):
    cid = _called(ctx)
    s = run_stage4(ctx, FakeLLM(fail_on={"=== TRANSCRIPT ==="}))
    assert s.failed == [cid]
    assert ctx.index.get(cid).stages["stage4_evaluation"].status == "failed"
    assert [f["stage"] for f in ctx.config.store.failures()] == ["stage4_evaluation"]


def test_old_index_entries_gain_stage4(ctx):
    old = {"candidate_id": "c1", "source_file": "x.docx", "source_sha256": None, "ingested_at": "t",
           "updated_at": "t", "stages": {s: {"status": "pending"} for s in
                                         ("stage1_extraction", "stage2_shortlisting", "stage3_calling")}}
    e = CandidateEntry.model_validate(old)
    assert list(e.stages) == ["stage1_extraction", "stage2_shortlisting", "stage3_calling", "stage4_evaluation"]
    ctx.config.store.save_index({"schema_version": "1.0", "updated_at": "t", "candidates": {"c1": old}})
    assert CandidateIndex(ctx.config.store).get("c1").stages["stage4_evaluation"].status == "pending"


def test_evaluation_weights_are_validated():
    with pytest.raises(ValueError, match="sum to 1.0"):
        EvaluationConfig(competency_weights={"role_knowledge": .5, "problem_solving": .5, "communication": .5,
                                             "motivation": .5})
    with pytest.raises(ValueError, match="resume_weight"):
        EvaluationConfig(resume_weight=.5, interview_weight=.6)
