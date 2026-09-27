"""Live interview sessions for the web server.

* One active session per invite link; starting again abandons the previous one.
* Every turn is persisted (sessions/<session_id> in the store), so a
  server crash never loses a transcript.
* When a session ends it is finalized (transcript -> LLM parse -> stage3 record)
  on a background thread, so the candidate's page is never kept waiting.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from ..context import RunContext
from ..llm import LLMClient
from ..mailer import Mailer, make_mailer, mask_email
from ..schemas import CallChannel, utc_now
from .dialogue import ScreeningDialogue
from .invites import InviteError, InviteStore
from .notify import call_minutes, candidate_contact
from .verification import VerificationError, check_code, has_access, send_code, verification_required


@dataclass
class _Live:
    token: str
    dialogue: ScreeningDialogue
    lock: threading.Lock
    last_active: float = field(default_factory=time.monotonic)

INTERRUPTED = ("abandoned", "failed")  # outcomes that mean "the call stopped partway"


class InterviewService:
    def __init__(self, ctx: RunContext, llm_factory: Callable[[], LLMClient],
                 mailer_factory: Callable[[], Mailer] | None = None):
        self.ctx = ctx
        self._llm_factory = llm_factory
        self._llm: LLMClient | None = None
        self._mailer_factory = mailer_factory or (lambda: make_mailer(ctx.config))
        self._mailer: Mailer | None = None
        self.invites = InviteStore(ctx.config.store)
        self._live: dict[str, _Live] = {}
        self._lock = threading.Lock()          # guards _live
        self._threads: list[threading.Thread] = []

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = self._llm_factory()
        return self._llm

    @property
    def mailer(self) -> Mailer:
        if self._mailer is None:
            self._mailer = self._mailer_factory()  # ConfigError if SMTP is not set up
        return self._mailer

    # -------------------------------------------------------------- candidate-facing

    def info(self, token: str) -> dict:
        """What the landing page may show. Deliberately minimal: first name, role, company,
        and (if a code is needed) the masked email address it goes to."""
        inv = self.invites.validate(token)
        entry = self._entry(inv.candidate_id)
        cfg = self._config(entry)
        s3 = cfg.settings.stage3
        job = cfg.job
        name = entry.display_name or ""
        _, email = self._contact(entry)
        need = verification_required(cfg, email)
        if inv.opened_at is None:
            self.invites.update(token, opened_at=utc_now())
        return {"first_name": name.split()[0] if name else None, "job_title": job.title,
                "company_name": job.company_name, "speech_lang": s3.speech_lang,
                "questions": len(cfg.questions.questions), "minutes": call_minutes(cfg),
                "verification_required": need, "email_hint": mask_email(email) if need else None}

    def request_code(self, token: str) -> dict:
        inv = self.invites.validate(token)
        name, email = self._contact(self._entry(inv.candidate_id))
        if not verification_required(self.ctx.config, email):
            raise VerificationError("No verification is needed for this link.", 400)
        result = send_code(self._config(self._entry(inv.candidate_id)), self.invites, inv, name=name, email=email, mailer=self.mailer)
        self.ctx.logger.info("stage3 agent: verification code sent for candidate_id=%s", inv.candidate_id)
        return result

    def verify_code(self, token: str, code: str) -> dict:
        inv = self.invites.validate(token)
        key = check_code(self.ctx.config, self.invites, inv, code)
        self.ctx.logger.info("stage3 agent: candidate_id=%s verified their email", inv.candidate_id)
        return {"access_key": key}

    def start(self, token: str, channel: CallChannel, access_key: str | None = None) -> dict:
        inv = self.invites.validate(token)
        entry = self._entry(inv.candidate_id)
        if entry.fraud_blocked:
            raise InviteError("this link is no longer active")
        full_name, email = self._contact(entry)
        if verification_required(self.ctx.config, email) and not has_access(inv, access_key):
            raise VerificationError("Please confirm the code we emailed you before starting.", 403)
        name = full_name or entry.display_name
        with self._lock:
            for sid, live in list(self._live.items()):
                if live.token == token:  # a reload / second tab: the older session is abandoned
                    self._close(sid, live)
            dialogue = ScreeningDialogue(config=self._config(entry), llm=self.llm, candidate_id=inv.candidate_id,
                                         candidate_name=name, channel=channel, logger=self.ctx.logger)
            live = _Live(token=token, dialogue=dialogue, lock=threading.Lock())
            self._live[dialogue.state.session_id] = live
        say = dialogue.start()
        self.invites.update(token, add_session=dialogue.state.session_id)
        self._persist(dialogue)
        self.ctx.logger.info("stage3 agent: session=%s started for candidate_id=%s via %s",
                             dialogue.state.session_id, inv.candidate_id, channel)
        return {"session_id": dialogue.state.session_id, "say": say, "ended": False}

    def turn(self, token: str, session_id: str, text: str) -> dict:
        live = self._get(token, session_id)
        live.last_active = time.monotonic()
        with live.lock:  # one turn at a time per session (double-submits are serialised)
            if live.dialogue.ended:
                return {"say": "", "ended": True, "outcome": live.dialogue.state.outcome}
            say = live.dialogue.reply(text)
            self._persist(live.dialogue)
            ended = live.dialogue.ended
        if ended:
            with self._lock:
                self._live.pop(session_id, None)
            self._finalize_async(live)
        return {"say": say, "ended": ended, "outcome": live.dialogue.state.outcome}

    def hang_up(self, token: str, session_id: str) -> dict:
        try:
            live = self._get(token, session_id)
        except InviteError:
            return {"ended": True}
        with self._lock:
            self._close(session_id, live)
        return {"ended": True, "outcome": live.dialogue.state.outcome}

    # -------------------------------------------------------------- internals

    def _entry(self, candidate_id: str):
        with self.ctx.lock:
            return self.ctx.index.reload().get(candidate_id).model_copy(deep=True)

    def _config(self, entry):
        """The config of the job this candidate applied to."""
        return self.ctx.config.for_job(entry.job_id)

    def _contact(self, entry) -> tuple[str | None, str | None]:
        try:
            return candidate_contact(self.ctx.config, entry)
        except Exception as e:  # unreadable Stage 1 record: no email known
            self.ctx.logger.warning("stage3 agent: candidate_id=%s no contact details: %s", entry.candidate_id, e)
            return entry.display_name, None

    def _get(self, token: str, session_id: str) -> _Live:
        with self._lock:
            live = self._live.get(session_id)
        if live is None or live.token != token:
            raise InviteError("this conversation is no longer active — please reload the page to start again")
        return live

    def _close(self, session_id: str, live: _Live) -> None:
        """Caller holds self._lock."""
        self._live.pop(session_id, None)
        with live.lock:
            if not live.dialogue.ended:
                live.dialogue.hang_up()
                self._persist(live.dialogue)
        self._finalize_async(live)

    def _persist(self, dialogue: ScreeningDialogue) -> None:
        self.ctx.config.store.put_record("sessions", dialogue.state.session_id, dialogue.to_dict())

    def _finalize_async(self, live: _Live) -> None:
        from ..stage3_call import finalize_dialogue

        def work():
            with self.ctx.lock:
                cid = live.dialogue.state.candidate_id
                try:
                    finalize_dialogue(self.ctx, self.llm, live.dialogue, self.invites, live.token)
                    self._schedule_interrupted_email(live)
                    # A completed call is scored straight away, so the dashboard fills in while the
                    # candidate is still on the thank-you page (run_stage4 log-and-skips on its own).
                    if self.ctx.index.reload().get(cid).stages["stage3_calling"].status == "success":
                        from ..stage4_evaluate import run_stage4

                        run_stage4(self.ctx, self.llm, only={cid})
                except Exception as e:  # both steps already log-and-skip; this is a last resort
                    self.ctx.logger.exception("stage3 agent: finalize crashed for session=%s: %s",
                                              live.dialogue.state.session_id, e)

        t = threading.Thread(target=work, name=f"finalize-{live.dialogue.state.session_id[:8]}", daemon=True)
        self._threads.append(t)
        t.start()

    # -------------------------------------------------------------- interrupted calls

    def _schedule_interrupted_email(self, live: _Live) -> None:
        """The candidate had started but the call stopped partway: email them the link to finish it,
        once the grace period shows they didn't simply reload the page and carry on."""
        st = live.dialogue.state
        s3 = self.ctx.config.settings.stage3
        if not s3.email_on_interrupted_call or (st.outcome or "abandoned") not in INTERRUPTED:
            return
        if not any(t.speaker == "candidate" for t in st.turns):
            return
        t = threading.Timer(s3.interrupted_grace_seconds, self._send_interrupted_email, args=(live.token, st.candidate_id))
        t.daemon = True
        self._threads.append(t)
        t.start()

    def _send_interrupted_email(self, token: str, cid: str) -> None:
        from .notify import email_interrupted_call

        try:
            with self._lock:
                if any(l.token == token for l in self._live.values()):
                    return  # they're back in a call already
            inv = self.invites.get(token)
            if inv is None or inv.status != "active" or inv.expired():
                return
            if inv.interrupted_emails >= self.ctx.config.settings.stage3.max_interrupted_emails:
                return
            with self.ctx.lock:
                entry = self.ctx.index.reload().get(cid)
                if entry.stages["stage3_calling"].status == "success" or entry.fraud_blocked:
                    return
                n = email_interrupted_call(self._config(entry), self.invites, entry, inv, self.mailer, self.ctx.logger)
                self.ctx.index.set_notification(cid, "screening", n)
        except Exception as e:  # e.g. email not configured: never breaks the interview service
            self.ctx.logger.error("stage3 agent: interrupted-call email for candidate_id=%s failed: %s", cid, e)

    def close_idle(self, now: float | None = None) -> int:
        """Close calls with no activity for stage3.idle_minutes (the candidate's connection dropped).
        Returns how many were closed; each one is finalized as interrupted."""
        limit = self.ctx.config.settings.stage3.idle_minutes * 60
        now = time.monotonic() if now is None else now
        with self._lock:
            idle = [(sid, l) for sid, l in self._live.items() if now - l.last_active >= limit]
            for sid, live in idle:
                self.ctx.logger.info("stage3 agent: session=%s idle, closing it as interrupted", sid)
                self._close(sid, live)
        return len(idle)

    def start_idle_sweeper(self, every_seconds: float = 30) -> None:
        def loop():
            while True:
                time.sleep(every_seconds)
                try:
                    self.close_idle()
                except Exception as e:
                    self.ctx.logger.error("stage3 agent: idle sweep failed: %s", e)

        threading.Thread(target=loop, name="idle-calls", daemon=True).start()

    def wait_for_finalizers(self, timeout: float = 60) -> None:
        for t in list(self._threads):
            t.join(timeout)

    def shutdown(self) -> None:
        """Server stopping: close live sessions (as abandoned) and let finalizers finish."""
        with self._lock:
            for sid, live in list(self._live.items()):
                self._close(sid, live)
        self.wait_for_finalizers()
