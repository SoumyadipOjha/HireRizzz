"""Approval gates: recruiter shortlist -> invites / rejection emails; manager final lists -> result emails."""

from __future__ import annotations

import csv
import io
import json
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer

import pytest
from conftest import FakeLLM, turn
from test_email import outbox

from screening.agent.dialogue import ScreeningDialogue
from screening.approvals import (
    ApprovalError,
    approve_final,
    approve_shortlist,
    awaiting_final_review,
    awaiting_shortlist_review,
    final_results,
    results_csv,
    send_final_results,
)
from screening.dashboard.server import DashboardAPI, Handler
from screening.stage0_ingest import ingest
from screening.stage1_extract import run_stage1
from screening.stage2_shortlist import run_stage2
from screening.stage3_call import WAITING_FOR_APPROVAL, finalize_dialogue, run_stage3
from screening.stage4_evaluate import run_stage4


@pytest.fixture
def gated(ctx):
    ctx.config.settings.approvals.require_shortlist_approval = True
    ingest(ctx)
    run_stage1(ctx, FakeLLM())
    run_stage2(ctx, FakeLLM())
    return ctx


def _ids(ctx):
    return {e.display_name: e.candidate_id for e in ctx.index.all()}


def _interview(ctx, cid, name):
    n = len(ctx.config.questions.questions)
    llm = FakeLLM(agent_script=[turn("consent_yes", True, "Great.")] + [turn("answer", True, "Thanks.")] * n)
    d = ScreeningDialogue(config=ctx.config, llm=llm, candidate_id=cid, candidate_name=name, channel="browser_text")
    d.start()
    for a in ["yes"] + [f"answer {i}" for i in range(n)]:
        if not d.ended:
            d.reply(a)
    finalize_dialogue(ctx, llm, d)
    run_stage4(ctx, FakeLLM(), only={cid})


def test_no_invites_before_the_recruiter_approves(gated):
    ctx = gated
    s3 = run_stage3(ctx)
    assert s3.awaiting == [] and outbox(ctx, "invite") == []
    assert all(e.stages["stage3_calling"].note == WAITING_FOR_APPROVAL for e in ctx.index.all())
    assert all(awaiting_shortlist_review(e) for e in ctx.index.all())


def test_accepting_ai_shortlist_invites_and_emails_rejections(gated):
    ctx = gated
    ids = _ids(ctx)
    ai = {cid: ctx.index.get(cid).stages["stage2_shortlisting"].decision for cid in ids.values()}
    r = approve_shortlist(ctx, ai, by="Riya (Recruiter)")
    assert r["invited"] == [ids["Aarav Sharma"]]
    assert sorted(r["rejected"]) == sorted([ids["Priya Nair"], ids["Rohan Mehta"]])
    assert [m["To"] for m in outbox(ctx, "invite")] == ["aarav@example.com"]
    assert sorted(m["To"] for m in outbox(ctx, "resume_rejected")) == ["priya@example.com", "rohan@example.com"]

    priya = ctx.index.get(ids["Priya Nair"])
    assert priya.overall_status == "rejected" and priya.notifications["shortlist"].status == "outbox"
    rev = ctx.index.get(ids["Aarav Sharma"]).reviews["shortlist"]
    assert rev.by == "Riya (Recruiter)" and rev.decision == rev.ai_decision == "shortlisted" and not rev.overridden
    assert not any(awaiting_shortlist_review(e) for e in ctx.index.all())


def test_recruiter_can_override_the_ai(gated):
    ctx = gated
    ids = _ids(ctx)
    assert ctx.index.get(ids["Priya Nair"]).stages["stage2_shortlisting"].decision == "rejected"
    r = approve_shortlist(ctx, {ids["Priya Nair"]: "shortlisted"}, by="Riya")
    assert r["invited"] == [ids["Priya Nair"]]
    entry = ctx.index.get(ids["Priya Nair"])
    assert entry.reviews["shortlist"].overridden and entry.overall_status == "awaiting"


def test_bad_approvals_are_refused(gated):
    ctx = gated
    ids = _ids(ctx)
    with pytest.raises(ApprovalError, match="who is approving"):
        approve_shortlist(ctx, {ids["Aarav Sharma"]: "shortlisted"}, by=" ")
    with pytest.raises(ApprovalError, match="decision must be"):
        approve_shortlist(ctx, {ids["Aarav Sharma"]: "maybe"}, by="Riya")
    with pytest.raises(ApprovalError, match="hasn't been evaluated"):
        approve_final(ctx, {ids["Aarav Sharma"]: "shortlisted"}, by="Manager")
    with pytest.raises(KeyError):
        approve_shortlist(ctx, {"00000000-0000-4000-8000-000000000000": "shortlisted"}, by="Riya")


def test_final_gate_lists_and_result_emails(gated):
    ctx = gated
    ids = _ids(ctx)
    approve_shortlist(ctx, {ids["Aarav Sharma"]: "shortlisted", ids["Priya Nair"]: "shortlisted"}, by="Riya")
    for name in ("Aarav Sharma", "Priya Nair"):
        _interview(ctx, ids[name], name)
    assert all(awaiting_final_review(ctx.index.get(ids[n])) for n in ("Aarav Sharma", "Priya Nair"))
    lists = final_results(ctx)
    assert len(lists["awaiting_approval"]) == 2 and lists["shortlisted"] == lists["rejected"] == []

    # nothing is emailed until the manager approves
    assert send_final_results(ctx) == {}
    approve_final(ctx, {ids["Aarav Sharma"]: "shortlisted", ids["Priya Nair"]: "rejected"}, by="Dev (Manager)")
    aarav, priya = ctx.index.get(ids["Aarav Sharma"]), ctx.index.get(ids["Priya Nair"])
    assert aarav.overall_status == "selected" and priya.overall_status == "not_selected"

    sent = send_final_results(ctx)
    assert sent == {ids["Aarav Sharma"]: "outbox", ids["Priya Nair"]: "outbox"}
    [sel], [rej] = outbox(ctx, "selected"), outbox(ctx, "not_selected")
    assert sel["To"] == "aarav@example.com" and "shortlisted" in sel.get_body(("plain",)).get_content()
    assert rej["To"] == "priya@example.com"
    assert send_final_results(ctx) == {}  # never emailed twice

    lists = final_results(ctx)
    assert [r["name"] for r in lists["shortlisted"]] == ["Aarav Sharma"]
    assert [r["name"] for r in lists["rejected"]] == ["Priya Nair"]
    assert lists["rejected"][0]["decided_by"] == "Dev (Manager)" and lists["rejected"][0]["email_status"] == "outbox"
    rows = list(csv.DictReader(io.StringIO(results_csv(lists))))
    assert [(r["list"], r["name"]) for r in rows] == [("shortlisted", "Aarav Sharma"), ("rejected", "Priya Nair")]


def test_rerunning_shortlisting_clears_approvals(gated):
    ctx = gated
    ids = _ids(ctx)
    approve_shortlist(ctx, {ids["Aarav Sharma"]: "shortlisted"}, by="Riya")
    run_stage2(ctx, FakeLLM(), force=True, only={ids["Aarav Sharma"]})
    assert "shortlist" not in ctx.index.get(ids["Aarav Sharma"]).reviews


# ---------------------------------------------------------------- over HTTP

@pytest.fixture
def dash(gated):
    ctx = gated
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, api=DashboardAPI(ctx.config, ctx=ctx)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", ctx
    httpd.shutdown()
    httpd.server_close()


def _post(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers or {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_approvals_over_http(dash):
    base, ctx = dash
    ids = _ids(ctx)
    body = {"by": "Riya", "decisions": {ids["Aarav Sharma"]: "shortlisted"}}

    # cross-site protections: a form/text post or a foreign Origin is refused, and nothing changes
    assert _post(base + "/api/approve/shortlist", body, {"Content-Type": "text/plain"})[0] == 403
    assert _post(base + "/api/approve/shortlist", body,
                 {"Content-Type": "application/json", "Origin": "https://evil.example"})[0] == 403
    assert "shortlist" not in ctx.index.reload().get(ids["Aarav Sharma"]).reviews

    status, r = _post(base + "/api/approve/shortlist", body)
    assert status == 200 and r["invited"] == [ids["Aarav Sharma"]]
    assert _post(base + "/api/approve/shortlist", {"by": "Riya", "decisions": {"not-an-id": "shortlisted"}})[0] == 400
    assert _post(base + "/api/approve/final", {"by": "M", "decisions": {ids["Aarav Sharma"]: "shortlisted"}})[0] == 400

    with urllib.request.urlopen(base + "/api/results.csv") as resp:
        assert resp.headers["Content-Disposition"].startswith('attachment; filename="final-results-')
        assert resp.read().decode("utf-8-sig").startswith("list,name,email,final_score")


def test_manager_can_overrule_the_pass_mark_and_change_a_decision(gated):
    ctx = gated
    ids = _ids(ctx)
    cid = ids["Aarav Sharma"]
    approve_shortlist(ctx, {cid: "shortlisted"}, by="Riya")
    _interview(ctx, cid, "Aarav Sharma")
    approve_final(ctx, {cid: "rejected"}, by="Dev (Manager)")
    assert send_final_results(ctx) == {cid: "outbox"} and len(outbox(ctx, "not_selected")) == 1
    [row] = final_results(ctx)["rejected"]
    assert row["email_current"] and row["email_kind"] == "not_selected"

    # the manager changes their mind: the change is recorded and the new result can be emailed
    r = approve_final(ctx, {cid: "shortlisted"}, by="Dev (Manager)")
    assert r["changed"] == [cid]
    review = ctx.index.get(cid).reviews["final"]
    assert review.decision == "shortlisted" and review.note.startswith("changed from rejected")
    [row] = final_results(ctx)["shortlisted"]
    assert not row["email_current"] and row["email_kind"] == "not_selected"  # the old result went out
    assert send_final_results(ctx) == {cid: "outbox"} and len(outbox(ctx, "selected")) == 1
    assert final_results(ctx)["shortlisted"][0]["email_current"]
    assert send_final_results(ctx) == {}  # and never twice for the same decision
