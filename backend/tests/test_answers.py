"""The candidate's own answers, question by question: stored with the Stage 3 record, shown to managers."""

from __future__ import annotations

from conftest import FakeLLM, turn

from screening.agent.answers import build_answers, turns_from_transcript
from screening.agent.dialogue import ScreeningDialogue
from screening.dashboard.server import DashboardAPI
from screening.schemas import Stage3Record
from screening.stage0_ingest import ingest
from screening.stage1_extract import run_stage1
from screening.stage2_shortlist import run_stage2
from screening.stage3_call import answers_for, finalize_dialogue, run_stage3


def _call(ctx, replies, script):
    ingest(ctx)
    run_stage1(ctx, FakeLLM())
    run_stage2(ctx, FakeLLM())
    run_stage3(ctx)
    aarav = next(e for e in ctx.index.all() if e.display_name == "Aarav Sharma")
    llm = FakeLLM(agent_script=script)
    d = ScreeningDialogue(config=ctx.config, llm=llm, candidate_id=aarav.candidate_id, candidate_name="Aarav Sharma",
                          channel="browser_text")
    d.start()
    for r in replies:
        if not d.ended:
            d.reply(r)
    ref = finalize_dialogue(ctx, llm, d)
    return aarav.candidate_id, Stage3Record.model_validate(ctx.config.store.get_record(ref)), d


def test_answers_are_stored_per_question_in_the_candidates_words(ctx):
    qs = ctx.config.questions.questions
    # q1 answered at once; q2 gets a follow-up (unresolved first), then the rest in one go
    script = ([turn("consent_yes", True, f"Great. {qs[0].question}"), turn("answer", True, f"Thanks. {qs[1].question}"),
               turn("answer", False, "Could you give a specific example?"), turn("answer", True, f"Nice. {qs[2].question}")]
              + [turn("answer", True, "Thanks.")] * (len(qs) - 2))
    replies = ["Sure, go ahead", "Yes, very interested", "I built APIs", "An order API in FastAPI, I owned payments",
               "Fixed a slow query with an index"] + [f"answer {i}" for i in range(3, len(qs))]
    cid, rec, _ = _call(ctx, replies, script)
    by = {a.question_id: a for a in rec.answers_verbatim}
    assert [a.question_id for a in rec.answers_verbatim] == [q.id for q in qs]  # every question, config order
    assert by["interest"].answer_text == "Yes, very interested" and by["interest"].mapping == "exact"
    proj = by["recent_project"]
    assert proj.answer_text == "I built APIs An order API in FastAPI, I owned payments" and proj.kind == "role"
    assert [t.speaker for t in proj.exchange] == ["agent", "candidate", "agent", "candidate"]
    assert proj.exchange[2].text == "Could you give a specific example?"  # the follow-up is kept
    assert by["problem_solved"].answer_text == "Fixed a slow query with an index"
    assert "Sure, go ahead" not in [a.answer_text for a in rec.answers_verbatim]  # consent isn't an answer
    assert by["interest"].ai_summary == "Yes"  # the parser's summary sits next to the real words

    detail = DashboardAPI(ctx.config, ctx=ctx).candidate(cid)
    assert detail["answers"][0]["answer_text"] == "Yes, very interested"
    [row] = DashboardAPI(ctx.config, ctx=ctx).all_answers()
    assert row["name"] == "Aarav Sharma" and row["call"]["status"] == "completed" and len(row["answers"]) == len(qs)


def test_older_calls_are_matched_from_the_agents_wording(ctx):
    qs = ctx.config.questions.questions
    transcript = "\n".join([
        "Agent: Hi, is now a good time?", "Candidate: yes",
        f"Agent: Great! {qs[0].question}", "Candidate: Yes I am open",
        "Agent: Could you tell me about a recent project where you built or maintained a REST API in Python?",
        "Candidate: I built an invoice service", "Agent: Can you share more detail?", "Candidate: FastAPI and Postgres",
        "Agent: What is your notice period?", "Candidate: 30 days",
    ])
    by = {a.question_id: a for a in build_answers(turns_from_transcript(transcript), qs)}
    assert by["interest"].answer_text == "Yes I am open" and by["interest"].mapping == "inferred"
    assert by["recent_project"].answer_text == "I built an invoice service FastAPI and Postgres"
    assert by["notice_period"].answer_text == "30 days"
    assert not by["expected_ctc"].answered and by["expected_ctc"].answer_text is None


def test_rebuilt_for_records_without_stored_answers(ctx):
    qs = ctx.config.questions.questions
    script = [turn("consent_yes", True, f"Great. {qs[0].question}")] + \
             [turn("answer", True, f"Thanks. {q.question}") for q in qs[1:]] + [turn("answer", True, "Bye.")]
    cid, rec, d = _call(ctx, ["yes"] + [f"answer {i}" for i in range(len(qs))], script)
    # an older call: its saved session has no question tags, and its record has no stored answers
    session = d.to_dict()
    for t in session["turns"]:
        t.pop("step", None)
    ctx.config.store.put_record("sessions", rec.call.session_id, session)
    old = rec.model_dump(mode="json")
    old.pop("answers_verbatim")
    rebuilt = answers_for(ctx.config, old)
    assert [a["answer_text"] for a in rebuilt][:3] == ["answer 0", "answer 1", "answer 2"]
    assert rebuilt[0]["mapping"] == "inferred"
