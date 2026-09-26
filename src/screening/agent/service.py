"""Live interview sessions for the web server.

* One active session per invite link; starting again abandons the previous one.
* Every turn is persisted to data/stage3_calls/sessions/<session_id>.json, so a
  server crash never loses a transcript.
* When a session ends it is finalized (transcript -> LLM parse -> stage3 record)
  on a background thread, so the candidate's page is never kept waiting.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from ..context import RunContext
from ..index import write_json_atomic
from ..llm import LLMClient
from ..schemas import CallChannel
from ..stage2_shortlist import load_stage1
from .dialogue import ScreeningDialogue
from .invites import InviteError, InviteStore


@dataclass
class _Live:
    token: str
    dialogue: ScreeningDialogue
    lock: threading.Lock


class InterviewService:
    def __init__(self, ctx: RunContext, llm_factory: Callable[[], LLMClient]):
        self.ctx = ctx
        self._llm_factory = llm_factory
        self._llm: LLMClient | None = None
        self.invites = InviteStore(ctx.config.data.stage3_invites)
        self._live: dict[str, _Live] = {}
        self._lock = threading.Lock()          # guards _live
        self._index_lock = threading.Lock()    # serialises index writes from finalizer threads
        self._threads: list[threading.Thread] = []

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = self._llm_factory()
        return self._llm

    # -------------------------------------------------------------- candidate-facing

    def info(self, token: str) -> dict:
        """What the landing page may show. Deliberately minimal: first name, role, company."""
        inv = self.invites.validate(token)
        entry = self._entry(inv.candidate_id)
        s3 = self.ctx.config.settings.stage3
        job = self.ctx.config.job
        name = entry.display_name or ""
        return {"first_name": name.split()[0] if name else None, "job_title": job.title,
                "company_name": job.company_name, "speech_lang": s3.speech_lang,
                "questions": len(self.ctx.config.questions.questions)}

    def start(self, token: str, channel: CallChannel) -> dict:
        inv = self.invites.validate(token)
        entry = self._entry(inv.candidate_id)
        name = load_stage1(entry).extraction.full_name if entry.stages["stage1_extraction"].status == "success" \
            else entry.display_name
        with self._lock:
            for sid, live in list(self._live.items()):
                if live.token == token:  # a reload / second tab: the older session is abandoned
                    self._close(sid, live)
            dialogue = ScreeningDialogue(config=self.ctx.config, llm=self.llm, candidate_id=inv.candidate_id,
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
        with self._index_lock:
            return self.ctx.index.reload().get(candidate_id).model_copy(deep=True)

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
        path = self.ctx.config.data.stage3_sessions / f"{dialogue.state.session_id}.json"
        write_json_atomic(path, dialogue.to_dict())

    def _finalize_async(self, live: _Live) -> None:
        from ..stage3_call import finalize_dialogue

        def work():
            with self._index_lock:
                try:
                    finalize_dialogue(self.ctx, self.llm, live.dialogue, self.invites, live.token)
                except Exception as e:  # finalize_dialogue already log-and-skips; this is a last resort
                    self.ctx.logger.exception("stage3 agent: finalize crashed for session=%s: %s",
                                              live.dialogue.state.session_id, e)

        t = threading.Thread(target=work, name=f"finalize-{live.dialogue.state.session_id[:8]}", daemon=True)
        self._threads.append(t)
        t.start()

    def wait_for_finalizers(self, timeout: float = 60) -> None:
        for t in list(self._threads):
            t.join(timeout)

    def shutdown(self) -> None:
        """Server stopping: close live sessions (as abandoned) and let finalizers finish."""
        with self._lock:
            for sid, live in list(self._live.items()):
                self._close(sid, live)
        self.wait_for_finalizers()
