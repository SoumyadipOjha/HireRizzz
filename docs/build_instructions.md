# build_instructions.md — Ordered build steps

> Derived from `reference_docs/kickoff_prompt.md` (original unavailable).
> Build strictly in order. Each step ends with a checkpoint.

## Step 0 — Scaffold & ingest
Build: folder structure (spec §2), `pyproject.toml`, `.env.example`, config
files, `paths.py`, `config.py` (with validation), `logging_setup.py`,
`index.py` (atomic writes), `schemas.py`, `prompts.py`, `docx_reader.py`,
`stage0_ingest.py`, CLI `ingest` + `status`, synthetic sample resumes.

**Checkpoint 0:** `screening ingest` on the 3 sample resumes → 3 UUID4
`candidate_id`s in `candidates_index.json`, all stages `pending`; re-running
ingest creates no duplicates; Word lock files are ignored; corrupt/non-docx
files still get an index row (and fail visibly in Stage 1).

## Step 1 — Stage 1: resume extraction
Build: `llm/base.py`, `llm/gemini.py` (JSON-schema constrained output),
`prompts/stage1_extraction/*`, `stage1_extract.py`, CLI `extract`, `check-llm`.
Validate LLM output with Pydantic; stamp envelope in code.

**Checkpoint 1:** one `stage1_extracted/<candidate_id>.json` per resume,
schema-valid; forced failure (e.g. too-short text / bad LLM JSON) is logged,
in `failures.jsonl`, and marked `failed` in the index; the batch continues.

## Step 2 — Stage 2: shortlisting
Build: `config/job_description.yaml`, `prompts/stage2_shortlisting/*`,
`stage2_shortlist.py`, CLI `shortlist`. LLM returns criterion scores; code
computes weighted score, applies threshold / must-have rule, writes decision.

**Checkpoint 2:** one `stage2_shortlist/<candidate_id>.json` per Stage-1
success, with decision + reasons; index shows `decision` and `score`;
candidates whose Stage 1 failed are marked `skipped`.

## Step 3 — Stage 3: in-house screening agent
(Originally a WAPI stub; on 2026-09-26 the owner asked to remove WAPI and build our own agent.)

Build: `agent/dialogue.py` (state machine), `agent/invites.py`, `agent/service.py`,
`prompts/stage3_calling/agent_*.md` + `parse_*.md`, `stage3_call.py`
(links + `finalize_dialogue`), `static/interview.html` (browser voice + text),
server routes, CLI `call`, `serve`, `simulate-call`, `parse-transcript`.

**Checkpoint 3:** `screening call` gives shortlisted candidates an `awaiting`
status and a link (re-run keeps the link; rejected → `skipped`); a full
conversation over HTTP produces a schema-valid `stage3_calls/<candidate_id>.json`,
marks Stage 3 `success` and the link used; hang-up keeps the link active;
opt-out / reschedule / LLM-failure paths end cleanly; the dashboard stays loopback-only.
