"""Invite / reminder / verification-code emails and the code step of the interview page."""

from __future__ import annotations

import email
import json
import re
import smtplib
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email import policy
from functools import partial
from http.server import ThreadingHTTPServer

import pytest
from conftest import PROFILES, FakeLLM

from screening.agent.invites import InviteStore
from screening.agent.notify import QR_CID, due_reminders, send_reminders
from screening.agent.service import InterviewService
from screening.agent.verification import VerificationError, check_code, send_code
from screening.dashboard.server import DashboardAPI, Handler
from screening.mailer import Email, EmailError, OutboxMailer, SmtpMailer, make_mailer, mask_email
from screening.stage0_ingest import ingest
from screening.stage1_extract import run_stage1
from screening.stage2_shortlist import run_stage2
from screening.stage3_call import run_stage3


def outbox(ctx, kind: str):
    """Messages of one kind (invite, reminder, otp, ...) written to <data>/outbox, oldest first."""
    folder = ctx.config.data.root / "outbox"
    name = re.compile(rf"^\d+T\d+_{re.escape(kind)}_[0-9a-f]{{8}}\.eml$")  # "selected" must not match "not_selected"
    files = sorted(f for f in folder.glob("*.eml") if name.match(f.name)) if folder.exists() else []
    return [email.message_from_bytes(f.read_bytes(), policy=policy.default) for f in files]


def _prepared(ctx):
    ingest(ctx)
    run_stage1(ctx, FakeLLM())
    run_stage2(ctx, FakeLLM())
    return next(e for e in ctx.index.all() if e.display_name == "Aarav Sharma")


def test_invite_email_has_link_button_and_inline_qr(ctx):
    aarav = _prepared(ctx)
    s3 = run_stage3(ctx)
    assert s3.awaiting == [aarav.candidate_id]
    [msg] = outbox(ctx, "invite")
    assert msg["To"] == "aarav@example.com"
    assert ctx.config.job.title in msg["Subject"]
    inv = InviteStore(ctx.config.store).active_for(aarav.candidate_id)
    url = f"{ctx.config.settings.stage3.public_base_url}/interview/{inv.token}"

    text = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert url in text and "Hi Aarav" in text
    assert f'href="{url}"' in html and "Start screening" in html
    assert f'src="cid:{QR_CID}"' in html
    images = [p for p in msg.walk() if p.get_content_type() == "image/png"]
    assert len(images) == 1 and images[0]["Content-ID"] == f"<{QR_CID}>"
    assert images[0].get_content()[:8] == b"\x89PNG\r\n\x1a\n"

    note = ctx.index.get(aarav.candidate_id).stages["stage3_calling"].note
    assert "invite outbox to a****@example.com" in note and note.endswith(url)
    stored = InviteStore(ctx.config.store).get(inv.token)
    assert stored.email_to == "aarav@example.com" and stored.emailed_at

    # re-running never emails the same link twice; --resend does
    run_stage3(ctx)
    assert len(outbox(ctx, "invite")) == 1
    run_stage3(ctx, resend=True)
    assert len(outbox(ctx, "invite")) == 2


def test_no_email_in_resume_still_issues_link(ctx, monkeypatch):
    monkeypatch.setitem(PROFILES["Aarav Sharma"], "email", None)
    aarav = _prepared(ctx)
    run_stage3(ctx)
    st = ctx.index.get(aarav.candidate_id).stages["stage3_calling"]
    assert st.status == "awaiting" and "NOT emailed (no valid email address in the resume)" in st.note
    assert outbox(ctx, "invite") == []


def test_smtp_not_configured_does_not_fail_stage3(ctx, monkeypatch):
    ctx.config.settings.email.mode = "smtp"
    monkeypatch.setenv("SMTP_USER", "")
    monkeypatch.setenv("SMTP_PASS", "")
    aarav = _prepared(ctx)
    s3 = run_stage3(ctx)
    assert s3.failed == [] and s3.awaiting == [aarav.candidate_id]
    assert "NOT emailed (email not configured)" in ctx.index.get(aarav.candidate_id).stages["stage3_calling"].note


def test_reminders_once_and_only_for_unstarted_calls(ctx):
    aarav = _prepared(ctx)
    run_stage3(ctx)
    invites = InviteStore(ctx.config.store)
    inv = invites.active_for(aarav.candidate_id)
    sent_at = datetime.fromisoformat(inv.emailed_at)
    mailer = make_mailer(ctx.config)

    assert send_reminders(ctx, mailer, now=sent_at + timedelta(hours=2)) == []
    assert send_reminders(ctx, mailer, now=sent_at + timedelta(hours=25)) == [aarav.candidate_id]
    [msg] = outbox(ctx, "reminder")
    assert msg["Subject"].startswith("Reminder") and msg["To"] == "aarav@example.com"
    assert send_reminders(ctx, mailer, now=sent_at + timedelta(hours=80)) == []  # max_reminders = 1

    ctx.config.settings.email.max_reminders = 3
    invites.update(inv.token, add_session="00000000-0000-4000-8000-000000000000")
    assert due_reminders(ctx.config, invites.all(), now=sent_at + timedelta(hours=80)) == []  # call started


def test_code_checks(ctx):
    aarav = _prepared(ctx)
    invites = InviteStore(ctx.config.store)
    inv = invites.create(aarav.candidate_id, ttl_days=3)
    mailer = OutboxMailer(ctx.config.data.root / "outbox", from_name="T", from_address="t@x.test")
    now = datetime.now(timezone.utc)
    r = send_code(ctx.config, invites, inv, name="Aarav Sharma", email="aarav@example.com", mailer=mailer, now=now)
    assert r["sent_to"] == "a****@example.com"
    with pytest.raises(VerificationError, match="just sent") as e:
        send_code(ctx.config, invites, invites.get(inv.token), name="A", email="aarav@example.com", mailer=mailer,
                  now=now + timedelta(seconds=5))
    assert e.value.status == 429
    code = re.search(r"\b(\d{6})\b", outbox(ctx, "otp")[0]["Subject"]).group(1)
    stored = invites.get(inv.token)
    assert code not in json.dumps(stored.model_dump())  # only a hash is stored

    with pytest.raises(VerificationError, match="expired"):
        check_code(ctx.config, invites, stored, code, now=now + timedelta(minutes=11))
    for i in range(5):
        with pytest.raises(VerificationError, match="isn't right"):
            check_code(ctx.config, invites, invites.get(inv.token), "000000" if code != "000000" else "111111")
    with pytest.raises(VerificationError, match="Too many attempts"):
        check_code(ctx.config, invites, invites.get(inv.token), code)


# ---------------------------------------------------------------- the code step over HTTP

@pytest.fixture
def site(ctx):
    aarav = _prepared(ctx)
    run_stage3(ctx)
    svc = InterviewService(ctx, llm_factory=lambda: FakeLLM())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, api=DashboardAPI(ctx.config), interviews=svc))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    token = InviteStore(ctx.config.store).active_for(aarav.candidate_id).token
    yield f"http://127.0.0.1:{httpd.server_address[1]}/api/interview/{token}", ctx, token
    httpd.shutdown()
    httpd.server_close()


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_verification_over_http(site):
    api, ctx, token = site
    with urllib.request.urlopen(api + "/info") as r:
        info = json.loads(r.read())
    assert info["verification_required"] is True and info["email_hint"] == "a****@example.com"
    assert InviteStore(ctx.config.store).get(token).opened_at

    status, body = _post(api + "/start", {"channel": "browser_text"})
    assert status == 403 and "code" in body["error"]

    status, body = _post(api + "/code", {})
    assert status == 200 and body["sent_to"] == "a****@example.com"
    code = re.search(r"\b(\d{6})\b", outbox(ctx, "otp")[-1]["Subject"]).group(1)
    assert _post(api + "/code", {})[0] == 429  # too soon for another code

    status, body = _post(api + "/verify", {"code": "12" if code != "12" else "13"})
    assert status == 400 and "attempts left" in body["error"]
    status, body = _post(api + "/verify", {"code": code})
    assert status == 200 and len(body["access_key"]) >= 32

    assert _post(api + "/start", {"channel": "browser_text", "access_key": "wrong-key"})[0] == 403
    status, body = _post(api + "/start", {"channel": "browser_text", "access_key": body["access_key"]})
    assert status == 200 and body["session_id"] and "Aarav" in body["say"]


# ---------------------------------------------------------------- SMTP client

class _FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=None, **kw):
        self.host, self.port, self.calls = host, port, []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        if password == "bad":
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        self.calls.append(("login", user))

    def send_message(self, msg):
        self.calls.append(("send", msg["To"], msg["From"]))


def test_smtp_mailer_uses_starttls_and_explains_auth_errors(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    mail = Email(to="c@example.com", subject="s", text="t", html="<p>h</p>")
    m = SmtpMailer(host="smtp.gmail.com", port=587, user="me@gmail.com", password="app-pass", security="starttls",
                   from_name="Talent Team", from_address="me@gmail.com")
    assert "sent to c@example.com" in m.send(mail)
    calls = _FakeSMTP.instances[-1].calls
    assert calls[0] == "starttls" and calls[1] == ("login", "me@gmail.com")
    assert calls[2] == ("send", "c@example.com", "Talent Team <me@gmail.com>")

    m.password = "bad"
    with pytest.raises(EmailError, match="app password"):
        m.send(mail)


def test_mask_email():
    assert mask_email("aarav@example.com") == "a****@example.com"
    assert mask_email("ab@x.io") == "a**@x.io"
