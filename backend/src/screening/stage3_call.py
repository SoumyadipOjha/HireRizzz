"""Stage 3 — screening call with the in-house agent.

`run_stage3` issues a private interview link to every shortlisted candidate
and emails it (QR code + button, agent/notify.py; status `awaiting`). The candidate talks to the agent in the browser (voice
or text, see agent/ and the dashboard server). When the conversation ends,
`finalize_dialogue` saves the transcript, parses it with the LLM and writes
data/stage3_calls/<candidate_id>.json — the same record as before.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .agent.dialogue import ScreeningDialogue
from .agent.answers import build_answers, turns_from_transcript
from .agent.invites import InviteStore
from .agent.notify import email_invite, interview_url  # noqa: F401  (interview_url re-exported)
from .config import AppConfig, ConfigError
from .context import RunContext, StageSummary
from .index import resume_decision
from .llm import LLMClient
from .mailer import Mailer, make_mailer, mask_email
from .prompts import load_prompt
from .storage import Store, load_model
from .schemas import CallInfo, LLMInfo, ScreeningAnswerLLM, Stage2Record, Stage3Record, TranscriptParseLLM
from .stage2_shortlist import JoinKeyMismatchError, load_stage1

STAGE = "stage3_calling"
UPSTREAM = "stage2_shortlisting"
PROMPT_DIR = "stage3_calling"
WAITING_FOR_APPROVAL = "waiting for a recruiter to approve the resume shortlist"


def _questions_block(config: AppConfig) -> str:
    return "\n".join(f"{i}. [{q.id}] {q.question}" for i, q in enumerate(config.questions.questions, 1))


def load_stage2(entry, store: Store) -> Stage2Record:
    st = entry.stages[UPSTREAM]
    record = load_model(store, st.output_path, Stage2Record)
    if record.candidate_id != entry.candidate_id:
        raise JoinKeyMismatchError(f"{st.output_path} has candidate_id {record.candidate_id}")
    return record


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------

def _invite_mailer(ctx: RunContext, mailer: Mailer | None) -> tuple[Mailer | None, str | None]:
    """(mailer, reason it's unavailable). Missing SMTP settings never stop Stage 3."""
    if mailer is not None:
        return mailer, None
    if not ctx.config.settings.email.send_invites:
        return None, "email.send_invites is off"
    try:
        return make_mailer(ctx.config), None
    except ConfigError as e:
        ctx.logger.warning("%s: invites will not be emailed: %s", STAGE, e)
        return None, "email not configured"


def run_stage3(ctx: RunContext, *, force: bool = False, only: set[str] | None = None,
               resend: bool = False, mailer: Mailer | None = None) -> StageSummary:
    """Give every shortlisted candidate an interview link and email it (QR + button).
    Re-running keeps existing active links and does not email them again; --resend
    re-emails active links; --force issues fresh ones (old links stop working)."""
    summary = StageSummary(STAGE)
    log = ctx.logger
    invites = InviteStore(ctx.config.store)
    ttl = ctx.config.settings.stage3.invite_ttl_days
    mailer, no_mail = _invite_mailer(ctx, mailer)

    for entry in ctx.index.all():
        cid = entry.candidate_id
        if only and cid not in only:
            continue
        st = entry.stages[STAGE]
        if st.status == "success" and not force:
            summary.already_done.append(cid)
            continue
        up = entry.stages[UPSTREAM]
        if up.status != "success":
            ctx.skip(STAGE, entry, f"{UPSTREAM} is '{up.status}', not 'success'")
            summary.skipped.append(cid)
            continue
        review = entry.reviews.get("shortlist")
        if review is None and ctx.config.settings.approvals.require_shortlist_approval:
            if st.note != WAITING_FOR_APPROVAL:
                ctx.skip(STAGE, entry, WAITING_FOR_APPROVAL)
            summary.skipped.append(cid)
            continue
        if resume_decision(entry) != "shortlisted":
            who = f"rejected by {review.by}" if review else f"decision={up.decision}, score={up.score}"
            ctx.skip(STAGE, entry, f"not shortlisted in {UPSTREAM} ({who})")
            summary.skipped.append(cid)
            continue
        if st.status == "skipped" and st.output_path and not force:
            summary.skipped.append(cid)  # opted out during an earlier call: never re-invite automatically
            continue

        try:
            s2 = load_stage2(entry, ctx.config.store)
            if review is None and s2.decision != "shortlisted":
                raise ValueError(f"index says shortlisted but {up.output_path} says {s2.decision}")
            inv = None if force else invites.active_for(cid)
            reused = inv is not None
            if inv is None:
                inv = invites.create(cid, ttl)
            url = interview_url(ctx.config, inv.token)
            expires = datetime.fromisoformat(inv.expires_at).strftime("%Y-%m-%d")
            if inv.emailed_at and not resend:
                mail_note = f"invite already emailed to {mask_email(inv.email_to)}" if inv.email_to else None
            elif mailer is None:
                mail_note = f"invite NOT emailed ({no_mail}); share the link manually"
            else:
                _, mail_note = email_invite(ctx.config, invites, entry, inv, mailer, log)
            # The note always ends with the URL (the CLI prints it from there).
            # Keep a partial record from an earlier rescheduled/abandoned call visible.
            note = f"interview link {'active' if reused else 'issued'} (expires {expires})"
            ctx.awaiting(STAGE, entry, f"{note}; {mail_note}: {url}" if mail_note else f"{note}: {url}",
                         output_path=st.output_path)
            summary.awaiting.append(cid)
        except Exception as e:
            ctx.fail(STAGE, entry, e)
            summary.failed.append(cid)

    log.info(summary.line())
    return summary


# ---------------------------------------------------------------------------
# Transcript parsing + record
# ---------------------------------------------------------------------------

def reconcile_answers(answers: list[ScreeningAnswerLLM], config: AppConfig, warn) -> list[ScreeningAnswerLLM]:
    """Exactly one answer per configured question, in config order."""
    by_id: dict[str, ScreeningAnswerLLM] = {}
    for a in answers:
        if a.question_id in by_id:
            warn(f"duplicate answer for question_id={a.question_id} (kept first)")
            continue
        by_id[a.question_id] = a
    known = {q.id for q in config.questions.questions}
    if unknown := set(by_id) - known:
        warn(f"answers for unknown question ids ignored: {sorted(unknown)}")
    out = []
    for q in config.questions.questions:
        a = by_id.get(q.id)
        if a is None:
            warn(f"no answer entry for question_id={q.id}; recorded as unanswered")
            a = ScreeningAnswerLLM(question_id=q.id, question=q.question, answered=False, answer_summary=None)
        out.append(a.model_copy(update={"question": q.question}))
    return out


def parse_transcript(ctx: RunContext, llm: LLMClient, *, candidate_id: str, candidate_name: str | None,
                     transcript: str) -> tuple[TranscriptParseLLM, str]:
    if not transcript.strip():
        raise ValueError("transcript is empty")
    system = load_prompt(PROMPT_DIR, "parse_system.md")
    template = load_prompt(PROMPT_DIR, "parse_transcript.md")
    prompt = template.render(candidate_name=candidate_name or "unknown", job_title=ctx.config.job.title,
                             questions=_questions_block(ctx.config), transcript=transcript.strip())
    parsed = llm.generate_json(system=system.text, prompt=prompt, schema=TranscriptParseLLM)
    warn = lambda m: ctx.logger.warning("%s: candidate_id=%s %s", STAGE, candidate_id, m)  # noqa: E731
    parsed = parsed.model_copy(update={"answers": reconcile_answers(parsed.answers, ctx.config, warn)})
    return parsed, template.rel_path


def save_transcript(ctx: RunContext, candidate_id: str, transcript: str) -> str:
    """Returns the transcript's ref."""
    return ctx.config.store.put_text("transcripts", candidate_id, transcript.strip() + "\n")


def write_stage3_record(ctx: RunContext, llm: LLMClient, *, candidate_id: str, call: CallInfo, transcript_ref: str,
                        parsed: TranscriptParseLLM, prompt_file: str, turns) -> str:
    """`turns`: the conversation (Turn objects or dicts) the per-question answers are built from."""
    record = Stage3Record(
        candidate_id=candidate_id, run_id=ctx.run_id,
        llm=LLMInfo(provider=llm.provider, model=llm.model, prompt_file=prompt_file),
        job_id=ctx.config.job.job_id, call=call, transcript_path=transcript_ref,
        screening=parsed, answers_verbatim=build_answers(turns, ctx.config.questions.questions, parsed.answers))
    return ctx.config.store.put_record("stage3_calls", candidate_id, record.model_dump(mode="json"))


def finalize_dialogue(ctx: RunContext, llm: LLMClient, dialogue: ScreeningDialogue,
                      invites: InviteStore | None = None, token: str | None = None) -> str | None:
    """Called once when a conversation ends (any outcome). Log-and-skip on errors.
    Returns the stage3 record's ref (None if nothing was written)."""
    st = dialogue.state
    cid = st.candidate_id
    entry = ctx.index.reload().get(cid)
    outcome = st.outcome or "abandoned"
    candidate_spoke = any(t.speaker == "candidate" for t in st.turns)

    # Invite lifecycle: completed -> used, opted out -> revoked, anything else -> stays active for a retry.
    # Calls held outside the web page (terminal simulation) close the candidate's active link too.
    if token is None and outcome in ("completed", "opted_out"):
        invites = invites or InviteStore(ctx.config.store)
        active = invites.active_for(cid)
        token = active.token if active else None
    if invites and token:
        if outcome == "completed":
            invites.update(token, status="used")
        elif outcome == "opted_out":
            invites.update(token, status="revoked")

    if not candidate_spoke:
        ctx.awaiting(STAGE, entry, f"last call {outcome} before the candidate said anything; link still active")
        return None

    try:
        tref = save_transcript(ctx, cid, dialogue.transcript_text())
        call = CallInfo(channel=st.channel, session_id=st.session_id, status=outcome, started_at=st.started_at,
                        ended_at=st.ended_at, duration_seconds=dialogue.duration_seconds(),
                        agent_turns=sum(t.speaker == "agent" for t in st.turns),
                        candidate_turns=sum(t.speaker == "candidate" for t in st.turns),
                        questions_asked=st.questions_asked, reschedule_note=st.reschedule_note,
                        agent_llm=f"{dialogue.llm.provider}/{dialogue.llm.model}")
        parsed, prompt_file = parse_transcript(ctx, llm, candidate_id=cid, candidate_name=st.candidate_name,
                                               transcript=dialogue.transcript_text())
        rel = write_stage3_record(ctx, llm, candidate_id=cid, call=call, transcript_ref=tref, parsed=parsed,
                                  prompt_file=prompt_file, turns=st.turns)
    except Exception as e:
        ctx.fail(STAGE, entry, e)
        return None

    if outcome == "completed":
        ctx.index.set_stage(cid, STAGE, "success", run_id=ctx.run_id, output_path=rel,
                            note=f"call completed ({st.channel})")
        ctx.logger.info("%s: candidate_id=%s call completed, wrote %s", STAGE, cid, rel)
    elif outcome == "opted_out":
        ctx.index.set_stage(cid, STAGE, "skipped", run_id=ctx.run_id, output_path=rel,
                            note="candidate opted out during the screening call; link revoked")
        ctx.logger.info("%s: candidate_id=%s OPTED OUT", STAGE, cid)
    else:
        extra = f" ({st.reschedule_note})" if st.reschedule_note else ""
        ctx.index.set_stage(cid, STAGE, "awaiting", run_id=ctx.run_id, output_path=rel,
                            note=f"last call {outcome}{extra}; link still active for another attempt")
    return rel


def parse_local_transcript(ctx: RunContext, llm: LLMClient, candidate_id: str, transcript_file: Path) -> str:
    """Run the transcript parser on a local .txt (e.g. a call held outside this system)."""
    entry = ctx.index.get(candidate_id)
    name = entry.display_name
    if entry.stages["stage1_extraction"].status == "success":
        name = load_stage1(entry, ctx.config.store).extraction.full_name
    transcript = Path(transcript_file).read_text(encoding="utf-8")
    parsed, prompt_file = parse_transcript(ctx, llm, candidate_id=candidate_id, candidate_name=name,
                                           transcript=transcript)
    tref = save_transcript(ctx, candidate_id, transcript)
    call = CallInfo(channel="local_transcript_file", session_id=None, status="completed")
    ref = write_stage3_record(ctx, llm, candidate_id=candidate_id, call=call, transcript_ref=tref, parsed=parsed,
                              prompt_file=prompt_file, turns=turns_from_transcript(transcript))
    ctx.index.set_stage(candidate_id, STAGE, "success", run_id=ctx.run_id, output_path=ref,
                        note="parsed from a local transcript file")
    return ref


def answers_for(ctx_or_config, record: dict) -> list[dict]:
    """The stored per-question answers of a Stage 3 record, or (older records) rebuilt from the saved
    session / transcript without changing anything."""
    config = getattr(ctx_or_config, "config", ctx_or_config)
    if record.get("answers_verbatim"):
        return record["answers_verbatim"]
    store = config.store
    sid = (record.get("call") or {}).get("session_id")
    session = store.get_record(store.ref("sessions", sid)) if sid else None
    turns = session.get("turns") if session else turns_from_transcript(store.get_text(record.get("transcript_path") or "") or "")
    parsed = [ScreeningAnswerLLM.model_validate(a) for a in (record.get("screening") or {}).get("answers", [])]
    return [a.model_dump() for a in build_answers(turns or [], config.questions.questions, parsed)]
