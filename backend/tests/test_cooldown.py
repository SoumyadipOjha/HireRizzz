"""Cooling period: an email that finished a screening call waits 30 days before another one (any job)."""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone

import pytest
from conftest import FakeLLM, turn

from screening.agent.dialogue import ScreeningDialogue
from screening.approvals import ApprovalError, approve_shortlist, board_column
from screening.cooldown import clear_cooling, cooling_active, find_recent_screening
from screening.stage0_ingest import ingest, resumes_dir
from screening.stage1_extract import run_stage1
from screening.stage2_shortlist import run_stage2
from screening.stage3_call import finalize_dialogue


def _screen(ctx, cid, name):
    n = len(ctx.config.questions.questions)
    llm = FakeLLM(agent_script=[turn("consent_yes", True, "Great.")] + [turn("answer", True, "Thanks.")] * n)
    d = ScreeningDialogue(config=ctx.config, llm=llm, candidate_id=cid, candidate_name=name, channel="browser_text")
    d.start()
    for a in ["yes"] + ["An answer."] * n:
        if not d.ended:
            d.reply(a)
    finalize_dialogue(ctx, llm, d)


@pytest.fixture
def screened(ctx):
    """Aarav has applied to the first job and finished his screening call."""
    ingest(ctx)
    run_stage1(ctx, FakeLLM())
    run_stage2(ctx, FakeLLM())
    aarav = next(e for e in ctx.index.all() if e.display_name == "Aarav Sharma")
    approve_shortlist(ctx, {aarav.candidate_id: "shortlisted"}, by="Asha")
    _screen(ctx, aarav.candidate_id, "Aarav Sharma")
    assert ctx.index.reload().get(aarav.candidate_id).stages["stage3_calling"].status == "success"

    first = ctx.config.jobs.get(None)
    job = {**first.job, "title": "Platform Engineer"}
    job.pop("job_id")
    second = ctx.config.jobs.create(job, first.questions).job["job_id"]
    folder = resumes_dir(ctx, second)
    folder.mkdir(parents=True)
    src = next(p for p in ctx.config.data.input_resumes.glob("*.docx") if "aarav" in p.name)
    shutil.copy(src, folder / "aarav_again.docx")
    new = ingest(ctx, job_id=second).new
    return ctx, aarav.candidate_id, new[0]


def test_the_same_email_is_held_for_30_days_on_any_job(screened):
    ctx, first_cid, again = screened
    run_stage1(ctx, FakeLLM())
    run_stage2(ctx, FakeLLM())
    e = ctx.index.reload().get(again)
    assert cooling_active(e) and e.overall_status == "cooling" and board_column(e) == "rejected"
    assert e.cooling.previous_candidate_id == first_cid and e.cooling.previous_job_title == ctx.config.job.title
    since, until = datetime.fromisoformat(e.cooling.since), datetime.fromisoformat(e.cooling.until)
    assert until - since == timedelta(days=30)
    assert e.stages["stage2_shortlisting"].status == "skipped" and "cooling period until" in e.stages["stage2_shortlisting"].note
    with pytest.raises(ApprovalError, match="cooling period"):
        approve_shortlist(ctx, {again: "shortlisted"}, by="Asha")
    # the person who was screened is not held by their own record
    assert not cooling_active(ctx.index.get(first_cid))
    # 30 days on, the same email can be screened again
    assert find_recent_screening(ctx, e, now=datetime.now(timezone.utc) + timedelta(days=31)) is None


def test_a_recruiter_can_let_them_through(screened):
    ctx, _, again = screened
    run_stage1(ctx, FakeLLM())
    with pytest.raises(ValueError, match="say why"):
        clear_cooling(ctx, again, by="Asha", note="")
    clear_cooling(ctx, again, by="Asha", note="Applying for a very different role")
    run_stage2(ctx, FakeLLM())
    e = ctx.index.reload().get(again)
    assert not cooling_active(e) and e.cooling.cleared.by == "Asha"
    assert e.stages["stage2_shortlisting"].status == "success" and board_column(e) == "resume_review"


def test_cooldown_can_be_switched_off(screened):
    ctx, _, again = screened
    ctx.config.settings.stage3.cooldown_days = 0
    run_stage1(ctx, FakeLLM())
    run_stage2(ctx, FakeLLM())
    assert ctx.index.reload().get(again).stages["stage2_shortlisting"].status == "success"
