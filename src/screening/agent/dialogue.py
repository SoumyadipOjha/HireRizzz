"""The in-house screening agent: a code-controlled dialogue with an LLM voice.

Code owns the structure — consent, then each configured question in order,
then close — plus follow-up limits, turn caps and failure fallbacks. The LLM,
once per candidate turn, classifies what the candidate said and writes the
next spoken line (prompts/stage3_calling/agent_*.md). So the conversation
cannot skip, reorder or invent questions, and a bad LLM turn cannot derail it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal

from ..config import AppConfig
from ..llm import LLMClient
from ..prompts import load_prompt
from ..schemas import AgentTurnLLM, CallChannel, CallOutcome, utc_now

PROMPT_DIR = "stage3_calling"
Phase = Literal["consent", "questions", "ended"]
# How many unclear consent replies before giving up politely.
MAX_CONSENT_ATTEMPTS = 3
# Consecutive LLM failures tolerated before ending the call gracefully.
MAX_LLM_FAILURES = 2


@dataclass
class Turn:
    speaker: Literal["agent", "candidate"]
    text: str
    at: str = field(default_factory=utc_now)


@dataclass
class DialogueState:
    session_id: str
    candidate_id: str
    candidate_name: str | None
    channel: CallChannel
    phase: Phase = "consent"
    q_index: int = 0
    unresolved_turns: int = 0          # candidate turns on the current step that did not resolve it
    consent_attempts: int = 0
    llm_failures: int = 0              # consecutive
    outcome: CallOutcome | None = None
    reschedule_note: str | None = None
    questions_asked: int = 0
    started_at: str = field(default_factory=utc_now)
    ended_at: str | None = None
    turns: list[Turn] = field(default_factory=list)


class ScreeningDialogue:
    def __init__(self, *, config: AppConfig, llm: LLMClient, candidate_id: str, candidate_name: str | None,
                 channel: CallChannel, logger: logging.Logger | None = None, state: DialogueState | None = None):
        self.config = config
        self.llm = llm
        self.log = logger or logging.getLogger("screening")
        self.s3 = config.settings.stage3
        self.questions = config.questions.questions
        self.state = state or DialogueState(session_id=str(uuid.uuid4()), candidate_id=candidate_id,
                                            candidate_name=candidate_name, channel=channel)
        job = config.job
        self._first = (candidate_name or "").split()[0] if candidate_name else "there"
        self._system = load_prompt(PROMPT_DIR, "agent_system.md").render(
            company_name=job.company_name, job_title=job.title,
            job_location=job.location or "location to be confirmed",
            candidate_name=candidate_name or "the candidate",
            job_summary=f"{job.title}. Must-have skills: {', '.join(job.must_have_skills)}.\n{job.description.strip()}",
            questions="\n".join(f"{i}. {q.question}" for i, q in enumerate(self.questions, 1)))
        self._turn_prompt = load_prompt(PROMPT_DIR, "agent_turn.md")

    # ------------------------------------------------------------------ public API

    @property
    def ended(self) -> bool:
        return self.state.phase == "ended"

    def start(self) -> str:
        if self.state.turns:
            raise RuntimeError("dialogue already started")
        job = self.config.job
        line = load_prompt(PROMPT_DIR, "agent_opening.md").render(
            candidate_first_name=self._first, company_name=job.company_name, job_title=job.title).strip()
        return self._agent(line)

    def reply(self, candidate_text: str) -> str:
        """Feed one candidate utterance; returns what the agent says next."""
        if self.ended:
            raise RuntimeError("dialogue has ended")
        text = " ".join((candidate_text or "").split())[: self.s3.max_turn_chars]
        if not text:  # silence / empty recognition: repeat the last line without recording it twice
            return self._agent(self._last_agent_line() or load_prompt(PROMPT_DIR, "agent_fallback.md").text.strip(),
                               log_only=True)
        self.state.turns.append(Turn("candidate", text))

        if self._candidate_turns() >= self.s3.max_candidate_turns:
            self.log.warning("stage3 agent: session=%s hit max_candidate_turns", self.state.session_id)
            return self._end("abandoned", self._closing())

        try:
            decision = self._ask_llm(text)
            self.state.llm_failures = 0
        except Exception as e:  # never crash a live conversation on an LLM hiccup
            self.state.llm_failures += 1
            self.log.error("stage3 agent: session=%s LLM turn failed (%d in a row): %s: %s",
                           self.state.session_id, self.state.llm_failures, type(e).__name__, e)
            if self.state.llm_failures >= MAX_LLM_FAILURES:
                return self._end("failed", load_prompt(PROMPT_DIR, "agent_failure.md").text.strip())
            self.state.turns.pop()  # candidate will repeat; keep the transcript clean
            return self._agent(load_prompt(PROMPT_DIR, "agent_fallback.md").text.strip(), log_only=True)

        return self._advance(decision)

    def hang_up(self) -> None:
        """Candidate closed the page / ended the call."""
        if not self.ended:
            self._end("abandoned", None)

    def transcript_text(self) -> str:
        return "\n".join(f"{'Agent' if t.speaker == 'agent' else 'Candidate'}: {t.text}" for t in self.state.turns)

    def to_dict(self) -> dict:
        return asdict(self.state)

    @classmethod
    def from_dict(cls, data: dict, *, config: AppConfig, llm: LLMClient, logger=None) -> "ScreeningDialogue":
        data = dict(data)
        data["turns"] = [Turn(**t) for t in data.get("turns", [])]
        state = DialogueState(**data)
        return cls(config=config, llm=llm, candidate_id=state.candidate_id, candidate_name=state.candidate_name,
                   channel=state.channel, logger=logger, state=state)

    def duration_seconds(self) -> float | None:
        if not self.state.ended_at:
            return None
        a = datetime.fromisoformat(self.state.started_at)
        b = datetime.fromisoformat(self.state.ended_at)
        return round((b - a).total_seconds(), 1)

    # ------------------------------------------------------------------ internals

    def _candidate_turns(self) -> int:
        return sum(1 for t in self.state.turns if t.speaker == "candidate")

    def _last_agent_line(self) -> str | None:
        return next((t.text for t in reversed(self.state.turns) if t.speaker == "agent"), None)

    def _agent(self, line: str, *, log_only: bool = False) -> str:
        if not log_only:
            self.state.turns.append(Turn("agent", line))
        return line

    def _closing(self) -> str:
        return load_prompt(PROMPT_DIR, "agent_closing.md").render(candidate_first_name=self._first).strip()

    def _end(self, outcome: CallOutcome, line: str | None) -> str:
        if line:
            self._agent(line)
        self.state.phase = "ended"
        self.state.outcome = outcome
        self.state.ended_at = utc_now()
        self.log.info("stage3 agent: session=%s candidate_id=%s ended outcome=%s", self.state.session_id,
                      self.state.candidate_id, outcome)
        return line or ""

    def _step_prompt(self) -> dict[str, str]:
        st, n, max_fu = self.state, len(self.questions), self.s3.max_followups_per_question
        if st.phase == "consent":
            first = self.questions[0].question
            return dict(
                current_step="CONSENT: find out whether now is a good time for a short screening chat.",
                step_guidance="",
                if_resolved=f'thank them briefly, then ask the first question in your own natural words: "{first}"',
                if_not_resolved="politely check again whether now is a good time to talk.",
            )
        q = self.questions[st.q_index]
        must_move_on = st.unresolved_turns >= max_fu
        guidance = f"Follow-ups already used on this question: {st.unresolved_turns} of {max_fu} allowed."
        if must_move_on:
            guidance += " Do NOT ask another follow-up: mark this step resolved and move on."
        if st.q_index + 1 < n:
            nxt = self.questions[st.q_index + 1].question
            if_resolved = f'acknowledge briefly, then ask the next question in your own natural words: "{nxt}"'
        else:
            if_resolved = ("acknowledge briefly, thank them for their time, say the recruiting team will review "
                           "this and be in touch soon, and say goodbye. Do not ask anything else.")
        return dict(
            current_step=f'QUESTION {st.q_index + 1} of {n} [{q.id}]: "{q.question}"',
            step_guidance=guidance,
            if_resolved=if_resolved,
            if_not_resolved='ask ONE short clarifying follow-up about the current question only '
                            '(e.g. "Just to confirm, is that thirty days or three months?").',
        )

    def _ask_llm(self, candidate_text: str) -> AgentTurnLLM:
        history = self.state.turns[:-1]  # the latest candidate message is passed separately
        conversation = "\n".join(f"{'Agent' if t.speaker == 'agent' else 'Candidate'}: {t.text}" for t in history)
        prompt = self._turn_prompt.render(conversation=conversation or "(none)", candidate_message=candidate_text,
                                          **self._step_prompt())
        return self.llm.generate_json(system=self._system, prompt=prompt, schema=AgentTurnLLM,
                                      temperature=self.s3.agent_temperature)

    def _advance(self, d: AgentTurnLLM) -> str:
        st = self.state
        say = " ".join(d.say.split()) or self._last_agent_line() or ""

        if d.candidate_intent == "opt_out":
            return self._end("opted_out", say)
        if d.candidate_intent == "reschedule":
            st.reschedule_note = d.reschedule_note or "candidate asked to talk another time"
            return self._end("rescheduled", say)

        if st.phase == "consent":
            if d.current_step_resolved or d.candidate_intent == "consent_yes":
                st.phase, st.q_index, st.unresolved_turns, st.questions_asked = "questions", 0, 0, 1
                return self._agent(say)
            st.consent_attempts += 1
            if st.consent_attempts >= MAX_CONSENT_ATTEMPTS:
                st.reschedule_note = "could not confirm a good time to talk"
                return self._end("rescheduled", "No problem, I'll let the recruiting team know to reach out "
                                               "at a better time. Thank you, goodbye!")
            return self._agent(say)

        # phase == questions
        max_fu = self.s3.max_followups_per_question
        resolved = d.current_step_resolved
        forced = False
        if not resolved:
            st.unresolved_turns += 1
            # One extra turn beyond the follow-up budget lets the candidate ask a question of their own.
            if st.unresolved_turns > max_fu + 1:
                resolved, forced = True, True
                self.log.warning("stage3 agent: session=%s forcing progress past question %s",
                                 st.session_id, self.questions[st.q_index].id)

        if not resolved:
            return self._agent(say)

        st.q_index += 1
        st.unresolved_turns = 0
        if st.q_index >= len(self.questions):
            return self._end("completed", self._closing() if forced else say)
        st.questions_asked += 1
        if forced:  # the LLM's line was a follow-up; replace it with the next question verbatim
            say = f"Thanks, let's move on. {self.questions[st.q_index].question}"
        return self._agent(say)
