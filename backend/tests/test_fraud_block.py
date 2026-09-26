"""'Fraud detected': red credibility issues stop a candidate before any further stage, until a recruiter clears them."""

from __future__ import annotations

import pytest
from conftest import PROFILES, FakeLLM

from screening.agent.invites import InviteError, InviteStore
from screening.agent.service import InterviewService
from screening.approvals import ApprovalError, approve_shortlist, awaiting_shortlist_review
from screening.credibility import check_resume, clear_fraud, save_record
from screening.stage0_ingest import ingest
from screening.stage1_extract import run_stage1
from screening.stage2_shortlist import FRAUD_NOTE, run_stage2
from screening.stage3_call import run_stage3


@pytest.fixture
def faker(ctx, monkeypatch):
    """Aarav claims 20 years of experience; his one dated role started in 2022 -> red issue."""
    monkeypatch.setitem(PROFILES["Aarav Sharma"], "total_experience_years", 20.0)
    ctx.config.settings.approvals.require_shortlist_approval = True
    ingest(ctx)
    run_stage1(ctx, FakeLLM())
    return ctx, next(e.candidate_id for e in ctx.index.all() if e.display_name == "Aarav Sharma")


def test_fraud_stops_the_candidate_at_every_layer(faker):
    ctx, cid = faker
    e = ctx.index.get(cid)
    assert e.fraud_blocked and e.overall_status == "fraud" and e.credibility.red >= 1

    run_stage2(ctx, FakeLLM())
    s2 = ctx.index.get(cid).stages["stage2_shortlisting"]
    assert s2.status == "skipped" and s2.note == FRAUD_NOTE           # never scored
    others = [x for x in ctx.index.all() if x.candidate_id != cid]
    assert all(x.stages["stage2_shortlisting"].status == "success" for x in others)  # the rest carry on

    assert not awaiting_shortlist_review(ctx.index.get(cid))          # not offered for approval
    with pytest.raises(ApprovalError, match="fraud detected"):
        approve_shortlist(ctx, {cid: "shortlisted"}, by="Riya")
    run_stage3(ctx)
    assert InviteStore(ctx.config.store).active_for(cid) is None     # no invite


def test_recruiter_can_clear_and_new_issues_stop_again(faker):
    ctx, cid = faker
    with pytest.raises(ValueError, match="who is clearing"):
        clear_fraud(ctx, cid, by="")
    e = clear_fraud(ctx, cid, by="Riya (Recruiter)", note="Counts freelance years; checked with the candidate")
    assert not e.fraud_blocked and e.fraud_cleared.by == "Riya (Recruiter)" and e.overall_status != "fraud"

    run_stage2(ctx, FakeLLM(), only={cid})                            # continues as normal
    assert ctx.index.get(cid).stages["stage2_shortlisting"].status == "success"
    approve_shortlist(ctx, {cid: "shortlisted"}, by="Riya")
    assert InviteStore(ctx.config.store).active_for(cid) is not None

    check_resume(ctx, cid)                                             # same issues again: stays cleared
    assert not ctx.index.get(cid).fraud_blocked

    rec = check_resume(ctx, cid)                                       # more red issues appear later
    rec.flags.append(rec.flags[0].model_copy(update={"message": "new contradiction"}))
    rec.summary.red += 1
    save_record(ctx, rec)
    e = ctx.index.get(cid)
    assert e.fraud_blocked and e.fraud_cleared is None                # stopped again...
    assert InviteStore(ctx.config.store).active_for(cid) is None      # ...and the open link is cancelled


def test_blocked_candidate_cannot_start_the_interview(faker):
    ctx, cid = faker
    clear_fraud(ctx, cid, by="Riya", note="ok")
    run_stage2(ctx, FakeLLM(), only={cid})
    approve_shortlist(ctx, {cid: "shortlisted"}, by="Riya")
    token = InviteStore(ctx.config.store).active_for(cid).token
    rec = check_resume(ctx, cid)
    rec.summary.red += 1                                               # e.g. a LinkedIn check after the invite
    save_record(ctx, rec)
    svc = InterviewService(ctx, llm_factory=lambda: FakeLLM())
    with pytest.raises(InviteError):
        svc.start(token, "browser_text")


def test_blocking_can_be_turned_off(ctx, monkeypatch):
    monkeypatch.setitem(PROFILES["Aarav Sharma"], "total_experience_years", 20.0)
    ctx.config.settings.credibility.block_on_fraud = False
    ingest(ctx)
    run_stage1(ctx, FakeLLM())
    e = next(x for x in ctx.index.all() if x.display_name == "Aarav Sharma")
    assert e.credibility.red >= 1 and not e.fraud_blocked             # flagged, but not stopped


# ---------------------------------------------------------------- clarification email to the candidate

def _clarifications(ctx):
    from test_email import outbox

    return outbox(ctx, "clarification")


def test_candidate_is_emailed_the_mismatches_once(faker):
    ctx, cid = faker
    [msg] = _clarifications(ctx)                                        # sent automatically when stopped
    assert msg["To"] == "aarav@example.com" and "update your resume and reapply" in msg["Subject"]
    text = msg.get_body(("plain",)).get_content()
    assert "Your resume mentions 20 years of experience" in text        # reworded for the candidate
    assert "correct these details in your resume and apply again" in text
    assert "where you found this role" in text                          # no apply_url set: generic wording
    assert "fraud" not in (text + msg.get_body(("html",)).get_content()).lower()  # never accuse
    note = ctx.index.get(cid).notifications["credibility"]
    assert note.kind == "clarification" and note.status == "outbox" and note.to == "aarav@example.com"

    check_resume(ctx, cid)                                              # same issues again: no second email
    assert len(_clarifications(ctx)) == 1


def test_manual_mode_only_emails_on_request(ctx, monkeypatch):
    from screening.credibility import request_clarification

    monkeypatch.setitem(PROFILES["Aarav Sharma"], "total_experience_years", 20.0)
    ctx.config.settings.credibility.email_candidate_on_fraud = "manual"
    ingest(ctx)
    run_stage1(ctx, FakeLLM())
    cid = next(e.candidate_id for e in ctx.index.all() if e.display_name == "Aarav Sharma")
    assert ctx.index.get(cid).fraud_blocked and _clarifications(ctx) == []
    n = request_clarification(ctx, cid)                                  # the recruiter's button
    assert n.status == "outbox" and len(_clarifications(ctx)) == 1


def test_reapply_button_links_to_the_job_posting(faker):
    from screening.credibility import request_clarification

    ctx, cid = faker
    ctx.config.job.apply_url = "https://careers.kanerika.com/jobs/python-backend?ref=<x>"
    request_clarification(ctx, cid)                                     # the recruiter's "Resend"
    msg = _clarifications(ctx)[-1]
    html = msg.get_body(("html",)).get_content()
    assert 'href="https://careers.kanerika.com/jobs/python-backend?ref=&lt;x&gt;"' in html   # escaped, clickable
    assert "Reapply for " in html and "&lt;table" not in html           # the button is real HTML
    assert "Reapply here: https://careers.kanerika.com/jobs/python-backend" in msg.get_body(("plain",)).get_content()
