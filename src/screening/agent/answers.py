"""The candidate's answers, question by question, in their own words.

Built by code from the conversation turns, never by the LLM:
* new calls: every candidate turn carries the id of the question that was open
  when they spoke (Turn.step), so the grouping is exact;
* calls recorded before that existed: each agent line is matched to the
  configured question it resembles most (word overlap), and the candidate's
  replies go to the question the agent last asked (mapping = "inferred").
"""

from __future__ import annotations

import re

from ..config import ScreeningQuestion
from ..schemas import CallAnswer, ExchangeTurn, ScreeningAnswerLLM

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "you", "your", "are", "is", "and", "or", "to", "of", "in", "for", "what", "do", "can",
         "could", "me", "i", "it", "that", "this", "with", "on", "how", "about", "tell", "would", "we", "our", "so"}


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}


def _best_question(line: str, questions: list[ScreeningQuestion], floor: float = 0.34) -> str | None:
    words = _words(line)
    best, best_score = None, 0.0
    for q in questions:
        qw = _words(q.question)
        if qw:
            score = len(words & qw) / len(qw)
            if score > best_score:
                best, best_score = q.id, score
    return best if best_score >= floor else None


def _turn_get(t, key):
    return t.get(key) if isinstance(t, dict) else getattr(t, key, None)


def build_answers(turns, questions: list[ScreeningQuestion],
                  summaries: list[ScreeningAnswerLLM] | None = None) -> list[CallAnswer]:
    """`turns`: dialogue Turn objects or their dicts (from a stored session)."""
    exact = any(_turn_get(t, "speaker") == "candidate" and _turn_get(t, "step") for t in turns)
    by_q: dict[str, list[ExchangeTurn]] = {q.id: [] for q in questions}
    ids = set(by_q)
    current = None      # question the agent is on (inferred mode) / last step seen (exact mode)
    pending_agent: list[ExchangeTurn] = []
    for t in turns:
        speaker, text, at = _turn_get(t, "speaker"), _turn_get(t, "text"), _turn_get(t, "at")
        et = ExchangeTurn(speaker=speaker, text=text, at=at)
        if speaker == "agent":
            if not exact:
                current = _best_question(text, questions) or current
            pending_agent.append(et)
            continue
        step = _turn_get(t, "step") if exact else current
        if step in ids:
            # the agent lines just before this reply are what the candidate was answering
            by_q[step].extend(a for a in pending_agent if a.text)
            by_q[step].append(et)
        pending_agent = []

    summary = {a.question_id: a for a in summaries or []}
    out = []
    for q in questions:
        ex = by_q[q.id]
        said = [e.text for e in ex if e.speaker == "candidate"]
        s = summary.get(q.id)
        out.append(CallAnswer(question_id=q.id, question=q.question, kind=q.kind, answered=bool(said),
                              answer_text=" ".join(said) if said else None, exchange=ex,
                              ai_summary=s.answer_summary if s and s.answered else None,
                              mapping="exact" if exact else "inferred"))
    return out


def turns_from_transcript(text: str) -> list[dict]:
    """'Agent: ...' / 'Candidate: ...' lines -> turn dicts (for transcripts without stored turns)."""
    out = []
    for line in (text or "").splitlines():
        for prefix, speaker in (("Agent:", "agent"), ("Candidate:", "candidate")):
            if line.startswith(prefix):
                out.append({"speaker": speaker, "text": line[len(prefix):].strip()})
    return out
