"""In-house screening agent: dialogue state machine, invites, HTTP interview flow."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer

import pytest
from conftest import FakeLLM, turn

from screening.agent.dialogue import ScreeningDialogue
from screening.agent.invites import InviteError, InviteStore
from screening.agent.service import InterviewService
from screening.dashboard.server import DashboardAPI, Handler
from screening.llm.base import LLMError
from screening.schemas import Stage3Record, Stage4Record
from screening.stage0_ingest import ingest
from screening.stage1_extract import run_stage1
from screening.stage2_shortlist import run_stage2
from screening.stage3_call import finalize_dialogue, run_stage3


def _dialogue(ctx, script):
    llm = FakeLLM(agent_script=script)
    return ScreeningDialogue(config=ctx.config, llm=llm, candidate_id="c1", candidate_name="Aarav Sharma",
                             channel="browser_text", logger=ctx.logger), llm


def _happy_script(n):
    return [turn("consent_yes", True, "Great. Are you open to new roles?")] + \
           [turn("answer", True, f"Thanks. Question {i + 2}?") for i in range(n - 1)] + \
           [turn("answer", True, "Thanks, that's everything. Goodbye!")]


# ---------------------------------------------------------------- dialogue


def test_opening_discloses_ai_and_recording(ctx):
    d, _ = _dialogue(ctx, [])
    line = d.start()
    assert "Hi Aarav" in line and "AI" in line and "recorded" in line
    assert ctx.config.job.company_name in line


def test_happy_path_asks_every_question_then_completes(ctx):
    n = len(ctx.config.questions.questions)
    d, llm = _dialogue(ctx, _happy_script(n))
    d.start()
    d.reply("yes sure")
    for i in range(n):
        assert not d.ended
        d.reply(f"answer {i}")
    assert d.ended and d.state.outcome == "completed"
    assert d.state.questions_asked == n
    # the per-turn prompt told the LLM exactly which question was current
    prompts = [p for name, p in llm.calls if name == "AgentTurnLLM"]
    for i, q in enumerate(ctx.config.questions.questions):
        assert f"QUESTION {i + 1} of {n} [{q.id}]" in prompts[i + 1]
    t = d.transcript_text().splitlines()
    assert t[0].startswith("Agent: Hi Aarav") and t[1] == "Candidate: yes sure"


def test_followup_budget_then_forced_progress(ctx):
    d, llm = _dialogue(ctx, [
        turn("consent_yes", True, "Great. First question?"),
        turn("unclear", False, "Could you clarify?"),     # follow-up 1 (allowed)
        turn("question", False, "Good question! So, are you open?"),  # candidate asked something (grace turn)
        turn("unclear", False, "One more clarification?"),  # LLM ignores the budget -> code forces progress
    ])
    d.start()
    d.reply("ok")
    d.reply("hmm")
    d.reply("what is the team size?")
    assert d.state.q_index == 0
    say = d.reply("mumble")
    assert d.state.q_index == 1
    assert say.endswith(ctx.config.questions.questions[1].question)  # verbatim next question, not the bad follow-up
    last_prompt = [p for name, p in llm.calls if name == "AgentTurnLLM"][-1]
    assert "Do NOT ask another follow-up" in last_prompt


@pytest.mark.parametrize("intent,outcome", [("opt_out", "opted_out"), ("reschedule", "rescheduled")])
def test_opt_out_and_reschedule_end_immediately(ctx, intent, outcome):
    d, _ = _dialogue(ctx, [turn(intent, False, "Understood, goodbye.", note="tomorrow 5pm")])
    d.start()
    d.reply("not now please")
    assert d.ended and d.state.outcome == outcome
    if outcome == "rescheduled":
        assert d.state.reschedule_note == "tomorrow 5pm"


def test_llm_outage_is_survived_then_ends_gracefully(ctx):
    d, _ = _dialogue(ctx, [LLMError("boom"), turn("consent_yes", True, "Great, first question?"),
                           LLMError("boom"), LLMError("boom again")])
    d.start()
    say = d.reply("yes")                       # 1st failure: apologise, candidate repeats
    assert "say that again" in say and not d.ended
    assert d.transcript_text().count("Candidate:") == 0  # failed turn not kept
    d.reply("yes")                             # recovers
    assert d.state.phase == "questions"
    d.reply("x")
    say = d.reply("x")                         # 2 consecutive failures -> graceful end
    assert d.ended and d.state.outcome == "failed" and "technical difficulties" in say


def test_consent_unclear_three_times_reschedules(ctx):
    d, _ = _dialogue(ctx, [turn("unclear", False, "Is now a good time?")] * 3)
    d.start()
    for _ in range(3):
        d.reply("???")
    assert d.state.outcome == "rescheduled"


def test_empty_input_repeats_without_polluting_transcript(ctx):
    d, _ = _dialogue(ctx, [])
    first = d.start()
    assert d.reply("   ") == first
    assert len(d.state.turns) == 1


def test_turn_cap_and_input_truncation(ctx):
    ctx.config.settings.stage3.max_candidate_turns = 6
    ctx.config.settings.stage3.max_turn_chars = 100
    d, _ = _dialogue(ctx, [turn("unclear", False, "Sorry?")] * 10)
    d.start()
    d.reply("x" * 500)
    assert len(d.state.turns[1].text) == 100
    while not d.ended:
        d.reply("x")
    assert d.state.outcome in ("abandoned", "rescheduled")


def test_state_roundtrip(ctx):
    d, llm = _dialogue(ctx, _happy_script(7))
    d.start()
    d.reply("yes")
    d2 = ScreeningDialogue.from_dict(json.loads(json.dumps(d.to_dict())), config=ctx.config, llm=llm)
    assert d2.state == d.state and d2.transcript_text() == d.transcript_text()


# ---------------------------------------------------------------- invites


def test_invites_lifecycle(ctx):
    store = InviteStore(ctx.config.store)
    path = ctx.config.data.stage3_invites
    a = store.create("cand-1", ttl_days=7)
    assert store.validate(a.token).candidate_id == "cand-1"
    b = store.create("cand-1", ttl_days=7)  # new link revokes the old one
    with pytest.raises(InviteError, match="no longer active"):
        store.validate(a.token)
    store.update(b.token, status="used")
    with pytest.raises(InviteError, match="already been completed"):
        store.validate(b.token)
    with pytest.raises(InviteError, match="invalid"):
        store.validate("../../etc/passwd")
    c = store.create("cand-2", ttl_days=1)
    raw = json.loads(path.read_text())
    raw["invites"][c.token]["expires_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(raw))
    with pytest.raises(InviteError, match="expired"):
        store.validate(c.token)


# ---------------------------------------------------------------- end to end over HTTP


@pytest.fixture
def live(ctx):
    ctx.config.settings.stage3.verify_email = False  # the code step has its own tests (test_email.py)
    ingest(ctx)
    run_stage1(ctx, FakeLLM())
    run_stage2(ctx, FakeLLM())
    run_stage3(ctx)
    n = len(ctx.config.questions.questions)
    llm = FakeLLM(agent_script=_happy_script(n))
    svc = InterviewService(ctx, llm_factory=lambda: llm)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, api=DashboardAPI(ctx.config), interviews=svc))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    aarav = next(e for e in ctx.index.all() if e.display_name == "Aarav Sharma")
    token = ctx.index.get(aarav.candidate_id).stages["stage3_calling"].note.rsplit("/", 1)[-1]
    yield f"http://127.0.0.1:{httpd.server_address[1]}", token, aarav.candidate_id, svc, ctx
    httpd.shutdown()
    httpd.server_close()


def _call(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return r.status, dict(r.headers), json.loads(r.read() or b"{}")


def test_interview_over_http(live):
    base, token, cid, svc, ctx = live
    with urllib.request.urlopen(f"{base}/interview/{token}") as r:
        page = r.read().decode()
        assert "Screening Call" in page
        assert r.headers["Referrer-Policy"] == "no-referrer"
        assert "microphone=(self)" in r.headers["Permissions-Policy"]

    _, _, info = _call(f"{base}/api/interview/{token}/info")
    assert info == {"first_name": "Aarav", "job_title": ctx.config.job.title,
                    "company_name": ctx.config.job.company_name, "speech_lang": "en-IN",
                    "questions": len(ctx.config.questions.questions), "minutes": 7,
                    "verification_required": False, "email_hint": None}  # nothing else leaks

    _, _, r = _call(f"{base}/api/interview/{token}/start", {"channel": "browser_voice"})
    sid = r["session_id"]
    assert "Hi Aarav" in r["say"]
    answers = ["Yes, go ahead", "Yes, actively looking",
               "I built an order API with FastAPI and PostgreSQL, I owned the payments endpoints",
               "A slow query: I used EXPLAIN, added a composite index and cut latency from 2s to 80ms",
               "Hyderabad", "Hybrid is fine", "Two months", "22 LPA", "30 LPA", "Weekday evenings"]
    for a in answers:
        _, _, r = _call(f"{base}/api/interview/{token}/turn", {"session_id": sid, "text": a})
        if r["ended"]:
            break
    assert r["ended"] and r["outcome"] == "completed"
    svc.wait_for_finalizers()

    entry = ctx.index.reload().get(cid)
    st = entry.stages["stage3_calling"]
    assert st.status == "success" and entry.overall_status == "evaluated"  # scored right after the call
    rec = Stage3Record.model_validate_json((ctx.config.data.stage3_output / f"{cid}.json").read_text())
    assert rec.call.channel == "browser_voice" and rec.call.status == "completed" and rec.call.session_id == sid
    assert rec.call.candidate_turns == len(answers)
    transcript = (ctx.config.data.stage3_transcripts / f"{cid}.txt").read_text()
    assert "Candidate: Hyderabad" in transcript
    assert (ctx.config.data.stage3_sessions / f"{sid}.json").exists()

    # the finished call was scored straight away (Stage 4)
    s4 = entry.stages["stage4_evaluation"]
    assert s4.status == "success" and s4.decision in ("shortlisted", "rejected") and entry.overall_status == "evaluated"
    rec4 = Stage4Record.model_validate_json((ctx.config.data.stage4_output / f"{cid}.json").read_text())
    role = next(c for c in rec4.competencies if c.competency == "role_knowledge")
    assert role.evidence_quotes == ["Yes, actively looking"]

    # link is single-use once completed
    with pytest.raises(urllib.error.HTTPError) as e:
        _call(f"{base}/api/interview/{token}/info")
    assert e.value.code == 410


def test_hang_up_keeps_link_and_records_partial(live):
    base, token, cid, svc, ctx = live
    _, _, r = _call(f"{base}/api/interview/{token}/start", {"channel": "browser_text"})
    sid = r["session_id"]
    _call(f"{base}/api/interview/{token}/turn", {"session_id": sid, "text": "yes"})
    _call(f"{base}/api/interview/{token}/end", {"session_id": sid})
    svc.wait_for_finalizers()
    st = ctx.index.reload().get(cid).stages["stage3_calling"]
    assert st.status == "awaiting" and "abandoned" in st.note and st.output_path
    assert _call(f"{base}/api/interview/{token}/info")[0] == 200  # can try again
    with pytest.raises(urllib.error.HTTPError) as e:  # the ended session can't be continued
        _call(f"{base}/api/interview/{token}/turn", {"session_id": sid, "text": "hello?"})
    assert e.value.code == 410


@pytest.mark.parametrize("path,body,code", [
    ("/api/interview/short/info", None, 404),
    ("/api/interview/" + "A" * 32 + "/info", None, 410),
    ("/api/interview/{t}/start", {"channel": "phone"}, 400),
    ("/api/interview/{t}/turn", {"session_id": "nope", "text": "x"}, 400),
    ("/api/interview/{t}/turn", {"session_id": "00000000-0000-4000-8000-000000000000", "text": "x"}, 410),
])
def test_interview_bad_requests(live, path, body, code):
    base, token, *_ = live
    with pytest.raises(urllib.error.HTTPError) as e:
        _call(base + path.replace("{t}", token), body)
    assert e.value.code == code


def test_finalize_without_candidate_speech_keeps_awaiting(ctx):
    ingest(ctx)
    run_stage1(ctx, FakeLLM())
    run_stage2(ctx, FakeLLM())
    run_stage3(ctx)
    aarav = next(e for e in ctx.index.all() if e.display_name == "Aarav Sharma")
    d = ScreeningDialogue(config=ctx.config, llm=FakeLLM(), candidate_id=aarav.candidate_id,
                          candidate_name="Aarav Sharma", channel="browser_voice")
    d.start()
    d.hang_up()
    assert finalize_dialogue(ctx, FakeLLM(), d) is None
    st = ctx.index.get(aarav.candidate_id).stages["stage3_calling"]
    assert st.status == "awaiting" and "before the candidate said anything" in st.note


def _emails(ctx, kind):
    from email import policy
    from email.parser import BytesParser

    out = ctx.config.data.root / "outbox"
    return [BytesParser(policy=policy.default).parsebytes(p.read_bytes())
            for p in sorted(out.glob(f"*_{kind}_*.eml"))] if out.exists() else []


def test_an_interrupted_call_emails_the_link_and_phone_number(live):
    import time

    base, token, cid, svc, ctx = live
    s3 = ctx.config.settings.stage3
    s3.interrupted_grace_seconds = 0
    invite = _emails(ctx, "invite")[-1].get_body(("plain",)).get_content()
    assert s3.support_phone == "+1 (463) 215-0098" and s3.support_phone in invite   # the phone is in the invite

    _, _, r = _call(f"{base}/api/interview/{token}/start", {"channel": "browser_text"})
    _call(f"{base}/api/interview/{token}/turn", {"session_id": r["session_id"], "text": "yes"})
    _call(f"{base}/api/interview/{token}/end", {"session_id": r["session_id"]})          # stops partway
    for _ in range(3):
        svc.wait_for_finalizers()
        time.sleep(0.2)
    [mail] = _emails(ctx, "call_halted")
    text = mail.get_body(("plain",)).get_content()
    assert "interrupted" in mail["Subject"] and token in text and "+1 (463) 215-0098" in text
    assert svc.invites.get(token).interrupted_emails == 1
    assert ctx.index.reload().get(cid).notifications["screening"].kind == "call_interrupted"

    # a call that never got past the greeting sends nothing; neither does an idle close once the cap is hit
    _, _, r = _call(f"{base}/api/interview/{token}/start", {"channel": "browser_text"})
    _call(f"{base}/api/interview/{token}/end", {"session_id": r["session_id"]})
    svc.wait_for_finalizers()
    time.sleep(0.2)
    assert len(_emails(ctx, "call_halted")) == 1


def test_idle_calls_are_closed_as_interrupted(live):
    import time

    base, token, cid, svc, ctx = live
    ctx.config.settings.stage3.interrupted_grace_seconds = 0
    _, _, r = _call(f"{base}/api/interview/{token}/start", {"channel": "browser_voice"})
    _call(f"{base}/api/interview/{token}/turn", {"session_id": r["session_id"], "text": "yes"})
    assert svc.close_idle() == 0                                          # still active
    assert svc.close_idle(now=time.monotonic() + 3600) == 1               # connection dropped long ago
    for _ in range(3):
        svc.wait_for_finalizers()
        time.sleep(0.2)
    assert ctx.index.reload().get(cid).stages["stage3_calling"].status == "awaiting"
    assert len(_emails(ctx, "call_halted")) == 1
