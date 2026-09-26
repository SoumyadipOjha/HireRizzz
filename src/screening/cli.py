"""Command-line entry point: `screening <command>` (or `python -m screening`)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .context import RunContext
from .index import CandidateIndexError, write_json_atomic
from .paths import SCHEMAS_DIR
from .schemas import EXPORTED_SCHEMAS, STAGES
from .storage import StoreError


def _ctx(args) -> RunContext:
    return RunContext.create(load_config(data_dir=args.data_dir, storage=args.storage, email=args.email))


def cmd_ingest(args) -> int:
    from .stage0_ingest import ingest

    ingest(_ctx(args), input_dir=args.input_dir)
    return 0


def _llm(ctx: RunContext):
    from .llm import make_client

    client = make_client(ctx.config)
    ctx.logger.info("LLM: %s / %s", client.provider, client.model)
    return client


def _only(args) -> set[str] | None:
    return set(args.only) if getattr(args, "only", None) else None


def _exit_code(*summaries) -> int:
    # 0 = batch ran (per-candidate failures are logged, not fatal); 1 = some candidate failed.
    return 1 if any(s.failed for s in summaries) else 0


def cmd_extract(args) -> int:
    from .stage1_extract import run_stage1

    ctx = _ctx(args)
    return _exit_code(run_stage1(ctx, _llm(ctx), force=args.force, only=_only(args)))


def cmd_shortlist(args) -> int:
    from .stage2_shortlist import run_stage2

    ctx = _ctx(args)
    ctx.config.job  # validate JD before touching candidates
    return _exit_code(run_stage2(ctx, _llm(ctx), force=args.force, only=_only(args)))


def _print_invites(ctx: RunContext, cids: list[str]) -> None:
    if not cids:
        return
    print()
    print("Interview links (each one is personal to that candidate):", flush=True)
    for cid in cids:
        e = ctx.index.get(cid)
        head, _, url = (e.stages["stage3_calling"].note or "").rpartition(": ")
        mail = head.split("; ", 1)[1] if "; " in head else ""
        print(f"  {e.display_name or cid:24} {url}", flush=True)
        if mail:
            print(f"  {'':24} {mail}", flush=True)
    print("Start the server so the links work:  uv run screening serve", flush=True)
    print()


def cmd_call(args) -> int:
    from .stage3_call import run_stage3

    ctx = _ctx(args)
    ctx.config.job, ctx.config.questions  # validate config before touching candidates
    s3 = run_stage3(ctx, force=args.force, only=_only(args), resend=args.resend)
    _print_invites(ctx, s3.awaiting)
    return _exit_code(s3)


def _decisions(args, entries, ai_decision) -> dict[str, str]:
    """--accept-ai takes the AI's suggestion for everyone waiting; --shortlist/--reject override per id."""
    out = {e.candidate_id: ai_decision(e) for e in entries} if args.accept_ai else {}
    for cid in args.shortlist or []:
        out[cid] = "shortlisted"
    for cid in args.reject or []:
        out[cid] = "rejected"
    return out


def cmd_review(args) -> int:
    from .approvals import awaiting_final_review, awaiting_shortlist_review

    ctx = _ctx(args)
    gates = [("Resume shortlist (recruiter)", awaiting_shortlist_review, "stage2_shortlisting",
              "approve-shortlist"),
             ("Final lists (hiring manager)", awaiting_final_review, "stage4_evaluation", "approve-final")]
    for title, waiting, stage, cmd in gates:
        rows = [e for e in ctx.index.all() if waiting(e)]
        print(f"{title}: {len(rows)} waiting")
        for e in rows:
            st = e.stages[stage]
            flag = "  NEEDS REVIEW" if (st.note or "").startswith("needs review") else ""
            print(f"  {e.candidate_id}  {(e.display_name or '-')[:24]:24} AI: {st.decision:11} score {st.score:g}{flag}")
        if rows:
            print(f"  -> uv run screening {cmd} --by \"Your Name\" --accept-ai   (or --shortlist/--reject ID)")
        print()
    return 0


def _approve(args, gate: str) -> int:
    from .approvals import ApprovalError, approve_final, approve_shortlist, awaiting_final_review, \
        awaiting_shortlist_review

    ctx = _ctx(args)
    ctx.config.job, ctx.config.questions
    waiting, stage = ((awaiting_shortlist_review, "stage2_shortlisting") if gate == "shortlist"
                      else (awaiting_final_review, "stage4_evaluation"))
    decisions = _decisions(args, [e for e in ctx.index.all() if waiting(e)], lambda e: e.stages[stage].decision)
    if not decisions:
        print("Nothing to approve: pass --accept-ai, or --shortlist / --reject with candidate ids.")
        return 0
    try:
        if gate == "shortlist":
            r = approve_shortlist(ctx, decisions, by=args.by, note=args.note)
            print(f"Recorded {r['recorded']} decision(s): {len(r['invited'])} invited, {len(r['rejected'])} rejected.")
            _print_invites(ctx, r["invited"])
        else:
            r = approve_final(ctx, decisions, by=args.by, note=args.note)
            print(f"Recorded {r['recorded']} final decision(s). Email the candidates with: uv run screening send-results")
    except ApprovalError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_approve_shortlist(args) -> int:
    return _approve(args, "shortlist")


def cmd_approve_final(args) -> int:
    return _approve(args, "final")


def cmd_send_results(args) -> int:
    from .approvals import send_final_results

    ctx = _ctx(args)
    ctx.config.job
    sent = send_final_results(ctx)
    for cid, status in sent.items():
        print(f"  {ctx.index.get(cid).display_name or cid:24} {status}")
    print(f"{sum(s in ('sent', 'outbox') for s in sent.values())} of {len(sent)} result email(s) sent")
    return 1 if any(s == "failed" for s in sent.values()) else 0


def cmd_results(args) -> int:
    from .approvals import final_results, results_csv

    ctx = _ctx(args)
    lists = final_results(ctx)
    if args.csv:
        Path(args.csv).write_text(results_csv(lists), encoding="utf-8-sig")
        print(f"wrote {args.csv}")
    for key, title in (("shortlisted", "FINAL SHORTLIST"), ("rejected", "REJECTED"),
                       ("awaiting_approval", "WAITING FOR MANAGER APPROVAL")):
        rows = lists[key]
        print(f"{title} ({len(rows)})")
        for r in rows:
            extra = " (overrode AI)" if r["overridden"] else ""
            extra += f"  email: {r['email_status']}" if r["email_status"] else ""
            print(f"  {(r['name'] or '-')[:24]:24} final {r['final_score']:>6g}  resume {r['resume_score']:>6g}  "
                  f"interview {r['interview_score']:>6g}  AI: {r['ai_suggestion']}{extra}")
        print()
    return 0


def cmd_write_jd(args) -> int:
    from .jd_writer import JDError, draft_jd

    ctx = _ctx(args)
    brief = Path(args.brief_file).read_text(encoding="utf-8") if args.brief_file else (args.brief or "")
    try:
        d = draft_jd(ctx.config, _llm(ctx), brief, company_name=args.company)
    except JDError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    job = d["job"]
    print(f"\nDRAFT (not live yet): {job['title']} · {job['company_name']} · {job.get('location') or '-'}")
    print(f"  must-have:    {', '.join(job['must_have_skills'])}")
    print(f"  nice-to-have: {', '.join(job['nice_to_have_skills']) or '-'}")
    for q in d["questions"]:
        if q["kind"] == "role":
            print(f"  role question: {q['question']}")
    for n in d["language_notes"]:
        print(f"  wording note:  {n}")
    print(f"\nEdit {ctx.config.data.rel(ctx.config.data.root / 'jd_draft.json')} if needed, then publish it:")
    print('  uv run screening approve-jd --by "Your Name (Hiring Manager)"')
    return 0


def cmd_approve_jd(args) -> int:
    from .jd_writer import JDError, approve_jd, load_draft

    ctx = _ctx(args)
    d = load_draft(ctx.config)
    if not d:
        print("No draft to approve. Create one with: uv run screening write-jd --brief \"...\"", file=sys.stderr)
        return 2
    scored = sum(e.stages["stage2_shortlisting"].status == "success" for e in ctx.index.all())
    try:
        r = approve_jd(ctx.config, d["job"], d["questions"], by=args.by, scored_candidates=scored)
    except JDError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(f"Published {r['title']} ({r['job_id']}) with {r['questions']} screening questions. Previous files: {r['backup']}")
    if scored:
        print(f"Note: {scored} candidate(s) were scored against the previous JD. Re-score them with "
              "`uv run screening shortlist --force`, or use a new --data-dir for this job.")
    return 0


def cmd_remind(args) -> int:
    from .agent.notify import send_reminders
    from .mailer import make_mailer

    ctx = _ctx(args)
    ctx.config.job, ctx.config.questions
    sent = send_reminders(ctx, make_mailer(ctx.config))
    print(f"{len(sent)} reminder(s) sent")
    return 0


def cmd_evaluate(args) -> int:
    from .stage4_evaluate import run_stage4

    ctx = _ctx(args)
    ctx.config.job, ctx.config.questions
    return _exit_code(run_stage4(ctx, _llm(ctx), force=args.force, only=_only(args)))


def cmd_run(args) -> int:
    from .stage0_ingest import ingest
    from .stage1_extract import run_stage1
    from .stage2_shortlist import run_stage2
    from .stage3_call import run_stage3
    from .stage4_evaluate import run_stage4

    ctx = _ctx(args)
    ctx.config.job, ctx.config.questions  # validate all config up front
    llm = _llm(ctx)
    ctx.logger.info("=== pipeline run %s ===", ctx.run_id)
    ingest(ctx, input_dir=args.input_dir)
    s1 = run_stage1(ctx, llm, force=args.force)
    s2 = run_stage2(ctx, llm, force=args.force)
    s3 = run_stage3(ctx, force=args.force)
    s4 = run_stage4(ctx, llm, force=args.force)  # candidates whose calls have finished
    ctx.logger.info("=== run complete ===")
    for s in (s1, s2, s3, s4):
        ctx.logger.info("  %s", s.line())
    _print_invites(ctx, s3.awaiting)
    return _exit_code(s1, s2, s3, s4)


def cmd_simulate_call(args) -> int:
    """Talk to the agent in the terminal (or replay a script of candidate lines)."""
    from .agent.dialogue import ScreeningDialogue
    from .stage2_shortlist import load_stage1
    from .stage3_call import finalize_dialogue

    ctx = _ctx(args)
    ctx.config.job, ctx.config.questions
    entry = ctx.index.get(args.candidate_id)
    if entry.stages["stage1_extraction"].status != "success":
        print("ERROR: this candidate has no Stage 1 profile yet", file=sys.stderr)
        return 2
    name = load_stage1(entry, ctx.config.store).extraction.full_name
    llm = _llm(ctx)
    script = None
    if args.script:
        script = [l.strip() for l in Path(args.script).read_text(encoding="utf-8").splitlines() if l.strip()]
    d = ScreeningDialogue(config=ctx.config, llm=llm, candidate_id=entry.candidate_id, candidate_name=name,
                          channel="terminal_simulation", logger=ctx.logger)
    print(f"--- simulated screening call with {name} (type 'quit' or press Ctrl+C to hang up) ---")
    print()
    print(f"Agent: {d.start()}", flush=True)
    print()
    try:
        while not d.ended:
            if script is not None:
                if not script:
                    d.hang_up()
                    break
                line = script.pop(0)
                print(f"You:   {line}")
            else:
                line = input("You:   ")
                if line.strip().lower() in ("quit", "exit"):
                    d.hang_up()
                    break
            print(f"Agent: {d.reply(line)}", flush=True)
            print()
    except (KeyboardInterrupt, EOFError):
        d.hang_up()
    print(f"--- call ended: {d.state.outcome} ---")
    out = finalize_dialogue(ctx, llm, d)
    if out:
        print(f"wrote {out}")
    return 0 if out or d.state.outcome != "completed" else 1


def cmd_parse_transcript(args) -> int:
    from .stage3_call import STAGE, parse_local_transcript

    ctx = _ctx(args)
    ctx.config.job, ctx.config.questions
    entry = ctx.index.get(args.candidate_id)
    try:
        out = parse_local_transcript(ctx, _llm(ctx), args.candidate_id, args.transcript_file)
    except Exception as e:
        ctx.fail(STAGE, entry, e)
        return 1
    ctx.logger.info("%s: candidate_id=%s wrote %s", STAGE, args.candidate_id, out)
    return 0


def cmd_check_llm(args) -> int:
    from .llm import make_client
    from .llm.base import LLMError

    cfg = load_config(data_dir=args.data_dir, storage=args.storage, email=args.email)
    client = make_client(cfg)
    print(f"provider={client.provider} configured model={client.model}")
    try:
        models = client.list_models()
    except LLMError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    ok = client.model in models
    print(f"API key works. {len(models)} text models available.")
    print(f"Configured model '{client.model}' is {'AVAILABLE' if ok else 'NOT AVAILABLE — change llm.model in config/settings.yaml'}")
    if not ok or args.verbose:
        print("Available:", ", ".join(m for m in models if "gemini" in m))
    return 0 if ok else 2


def cmd_dashboard(args) -> int:
    from .dashboard import serve

    serve(load_config(data_dir=args.data_dir, storage=args.storage, email=args.email), port=args.port, open_browser=not args.no_browser, host=args.host)
    return 0


def cmd_status(args) -> int:
    cfg = load_config(data_dir=args.data_dir, storage=args.storage, email=args.email)
    from .index import CandidateIndex

    index = CandidateIndex(cfg.store)
    entries = index.all()
    if args.json:
        print(json.dumps(index.doc.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return 0
    if not entries:
        print(f"No candidates in {cfg.data.candidates_index}")
        return 0
    short = {"stage1_extraction": "S1", "stage2_shortlisting": "S2", "stage3_calling": "S3", "stage4_evaluation": "S4"}
    print(f"{'candidate_id':36}  {'name':22} {'overall':9} " + " ".join(f"{short[s]:9}" for s in STAGES) + " file")
    for e in entries:
        stage_cols = []
        for s in STAGES:
            st = e.stages[s]
            label = st.status
            if s in ("stage2_shortlisting", "stage4_evaluation") and st.decision:
                label = f"{st.decision[:5]}:{st.score:g}" if st.score is not None else st.decision
            stage_cols.append(f"{label:9}")
        name = (e.display_name or "-")[:22]
        print(f"{e.candidate_id:36}  {name:22} {e.overall_status:9} " + " ".join(stage_cols) + f" {Path(e.source_file).name}")
        for s in STAGES:
            st = e.stages[s]
            if st.error or st.note:
                print(f"{'':38}{short[s]}: {st.error or st.note}")
    return 0


def cmd_export_schemas(args) -> int:
    SCHEMAS_DIR.mkdir(exist_ok=True)
    for name, model in EXPORTED_SCHEMAS.items():
        path = SCHEMAS_DIR / f"{name}.schema.json"
        write_json_atomic(path, model.model_json_schema())
        print(f"wrote {path.relative_to(SCHEMAS_DIR.parent).as_posix()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="screening", description="Recruiting screening pipeline (Stages 1-4).")
    p.add_argument("--data-dir", type=Path, default=None, help="Data directory (default: <project>/data)")
    p.add_argument("--storage", choices=("file", "mongodb"), default=None,
                   help="Where data is stored (default: STORAGE_BACKEND env var, else storage.backend in settings.yaml)")
    p.add_argument("--email", choices=("smtp", "outbox"), default=None,
                   help="smtp = send emails; outbox = write them to <data>/outbox as .eml files (dry run). "
                        "Default: EMAIL_MODE env var, else email.mode in settings.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("ingest", help="Stage 0: register resumes and assign candidate_ids")
    s.add_argument("--input-dir", type=Path, default=None, help="Folder of .docx resumes (default: data/input/resumes)")
    s.set_defaults(func=cmd_ingest)

    def stage_cmd(name, help_, func):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--force", action="store_true", help="Re-run candidates that already succeeded")
        s.add_argument("--only", action="append", metavar="CANDIDATE_ID", help="Limit to these candidate_ids")
        s.set_defaults(func=func)

    stage_cmd("extract", "Stage 1: extract structured profiles from resumes (LLM)", cmd_extract)
    stage_cmd("shortlist", "Stage 2: score profiles against the job and decide", cmd_shortlist)
    stage_cmd("call", "Stage 3: issue private interview links to shortlisted candidates and email them", cmd_call)
    sub.choices["call"].add_argument("--resend", action="store_true",
                                     help="Email active links again (e.g. after fixing SMTP settings)")

    stage_cmd("evaluate", "Stage 4: score finished interviews and suggest final decisions (LLM)", cmd_evaluate)

    s = sub.add_parser("write-jd", help="Gate 1: AI drafts a JD + role questions from a short brief (not live yet)")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--brief", help="What the role is: responsibilities, key skills, experience, location")
    g.add_argument("--brief-file", type=Path, help="Text file with the brief")
    s.add_argument("--company", default=None, help="Company name (default: the current JD's)")
    s.set_defaults(func=cmd_write_jd)
    s = sub.add_parser("approve-jd", help="Gate 1: hiring manager publishes the drafted JD + questions")
    s.add_argument("--by", required=True, help="Who is approving")
    s.set_defaults(func=cmd_approve_jd)

    s = sub.add_parser("review", help="Show candidates waiting at an approval gate")
    s.set_defaults(func=cmd_review)
    for name, func, help_ in (
            ("approve-shortlist", cmd_approve_shortlist,
             "Gate: recruiter approves the resume shortlist (invites / rejection emails go out)"),
            ("approve-final", cmd_approve_final, "Gate: hiring manager approves the final lists")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--by", required=True, help="Who is approving (recorded with each decision)")
        s.add_argument("--accept-ai", action="store_true", help="Accept the AI suggestion for everyone waiting")
        s.add_argument("--shortlist", action="append", metavar="CANDIDATE_ID", help="Shortlist this candidate")
        s.add_argument("--reject", action="append", metavar="CANDIDATE_ID", help="Reject this candidate")
        s.add_argument("--note", default=None, help="Optional note stored with the decisions")
        s.set_defaults(func=func)
    s = sub.add_parser("send-results", help="Email approved final decisions (selected / not selected)")
    s.set_defaults(func=cmd_send_results)
    s = sub.add_parser("results", help="Print the final shortlisted and rejected lists")
    s.add_argument("--csv", type=Path, default=None, help="Also write them to this CSV file")
    s.set_defaults(func=cmd_results)

    s = sub.add_parser("remind", help="Stage 3: email a reminder to candidates who haven't started their call")
    s.set_defaults(func=cmd_remind)

    s = sub.add_parser("run", help="Ingest + Stages 1-4 in order")
    s.add_argument("--input-dir", type=Path, default=None)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("simulate-call", help="Stage 3: talk to the screening agent in the terminal")
    s.add_argument("candidate_id")
    s.add_argument("--script", type=Path, help="Text file of candidate replies, one per line (non-interactive)")
    s.set_defaults(func=cmd_simulate_call)

    s = sub.add_parser("parse-transcript", help="Stage 3 transcript parser on a local .txt (call held elsewhere)")
    s.add_argument("candidate_id")
    s.add_argument("transcript_file", type=Path)
    s.set_defaults(func=cmd_parse_transcript)

    s = sub.add_parser("check-llm", help="Verify the API key and that the configured model exists")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_check_llm)

    for name in ("serve", "dashboard"):
        s = sub.add_parser(name, help="Start the web server: HR dashboard + candidate interview pages"
                           if name == "serve" else "Alias of `serve`")
        s.add_argument("--port", type=int, default=8765)
        s.add_argument("--host", default="127.0.0.1",
                       help="Interface to bind (default 127.0.0.1). The dashboard only ever answers loopback clients.")
        s.add_argument("--no-browser", action="store_true", help="Don't open a browser window")
        s.set_defaults(func=cmd_dashboard)

    s = sub.add_parser("status", help="Show candidates_index.json as a table")
    s.add_argument("--json", action="store_true", help="Print the raw index JSON")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("export-schemas", help="Write JSON Schemas for every output file to schemas/")
    s.set_defaults(func=cmd_export_schemas)
    return p


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page; resumes contain non-ASCII names.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, CandidateIndexError, StoreError, FileNotFoundError, KeyError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
