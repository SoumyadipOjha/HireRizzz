# spec.md — Recruiting Screening Pipeline (Stages 1–3)

> **Provenance.** The original `intent.md` / `spec.md` / `build_instructions.md`
> were not available. On 2026-09-26 the owner chose to "skip docs, just build",
> so this spec was **derived from `reference_docs/kickoff_prompt.md`** and is
> the contract the code implements. Every assumption made is listed in §9.
> If the original documents turn up, diff them against this file first.

---

## 1. Scope

| Stage | Name | In scope | Output |
|---|---|---|---|
| 0 | Ingest | ✅ | `candidate_id` assigned, row in `candidates_index.json` |
| 1 | Resume extraction (LLM) | ✅ | `data/stage1_extracted/<candidate_id>.json` |
| 2 | Shortlisting (LLM + deterministic score) | ✅ | `data/stage2_shortlist/<candidate_id>.json` |
| 3 | Screening call with the **in-house agent** (browser voice/text, Gemini-driven) + transcript parsing | ✅ | `data/stage3_calls/<candidate_id>.json` |
| 4 | HR filter | ❌ out of scope | — |

Single batch, sequential, no concurrency, no queue, **no retries**.
LLM provider: **Google Gemini** (free tier, `google-genai` SDK), behind a
provider-neutral interface.

---

## 2. Folder structure

```
recruiting_screening_pipeline/
├── pyproject.toml / uv.lock        # deps (uv-managed venv in .venv/)
├── .env.example                    # GEMINI_API_KEY=
├── README.md
├── config/
│   ├── settings.yaml               # paths, thresholds, weights, LLM settings
│   ├── job_description.yaml        # the role candidates are scored against
│   └── screening_questions.yaml    # questions the Stage 3 call agent asks
├── prompts/
│   ├── stage1_extraction/
│   │   ├── system.md
│   │   └── extract_resume.md
│   ├── stage2_shortlisting/
│   │   ├── system.md
│   │   └── score_candidate.md
│   └── stage3_calling/
│       ├── agent_system.md         # the live agent's persona + rules (system prompt)
│       ├── agent_turn.md           # per-turn prompt; code fills in the current step
│       ├── agent_opening.md        # fixed opening line (AI + recording disclosure, consent)
│       ├── agent_closing.md        # fallback goodbye when code forces the close
│       ├── agent_fallback.md       # "please repeat" after one LLM failure
│       ├── agent_failure.md        # graceful goodbye after repeated LLM failures
│       ├── parse_system.md
│       └── parse_transcript.md
├── schemas/                        # JSON Schema exports of §4 (generated: `screening export-schemas`)
├── data/
│   ├── input/resumes/              # drop .docx files here
│   ├── candidates_index.json       # master index / join table (§3)
│   ├── stage1_extracted/<candidate_id>.json
│   ├── stage2_shortlist/<candidate_id>.json
│   ├── stage3_calls/<candidate_id>.json
│   ├── stage3_calls/transcripts/<candidate_id>.txt
│   ├── stage3_calls/sessions/<session_id>.json   # every conversation, persisted after each turn
│   ├── stage3_calls/invites.json               # interview-link tokens (secrets)
│   └── logs/
│       ├── pipeline.log            # human-readable log (every run appends)
│       └── failures.jsonl          # one JSON line per FAILED candidate-stage (skips/blocks are in log + index)
├── samples/resumes/                # 3 synthetic test resumes (.docx)
├── samples/transcripts/            # sample call transcript for `parse-transcript`
├── scripts/make_sample_resumes.py  # regenerates samples/resumes
├── scripts/make_demo_data.py       # builds demo_runs/demo with a scripted fake LLM (DEMO_DATA.txt marker)
├── demo_runs/                      # scratch data dirs for demo runs (git-ignored)
├── src/screening/                  # code (§7)
├── tests/
└── docs/  intent.md · spec.md · build_instructions.md
```

`data/` paths are relative to the project root and can be redirected with
`--data-dir` (used for test/demo runs so real data is never polluted).

---

## 3. `candidate_id` and `candidates_index.json`

* `candidate_id` = **UUID4 string**, generated once at ingest. It is the join
  key for every stage: file names, JSON bodies, log lines, failures.
* The LLM **never** produces or sees a `candidate_id`; code stamps it on.
* Re-ingesting a byte-identical file (same SHA-256) re-uses the existing
  `candidate_id` — no duplicate candidates.
* Index is written atomically (temp file + `os.replace`) after every change.

```json
{
  "schema_version": "1.0",
  "updated_at": "2026-09-26T10:00:00+00:00",
  "candidates": {
    "<candidate_id>": {
      "candidate_id": "<uuid4>",
      "source_file": "data/input/resumes/jane_doe.docx",
      "source_sha256": "<hex>",
      "ingested_at": "<iso8601 utc>",
      "updated_at": "<iso8601 utc>",
      "display_name": "Jane Doe",          // null until Stage 1 succeeds
      "current_stage": "stage2_shortlisting",
      "overall_status": "active",          // active | rejected | failed | awaiting | completed
      "stages": {
        "stage1_extraction":   { "status": "success", "output_path": "...", "updated_at": "...", "error": null, "run_id": "..." },
        "stage2_shortlisting": { "status": "success", "decision": "shortlisted", "score": 82.5, "output_path": "...", ... },
        "stage3_calling":      { "status": "awaiting", "note": "interview link issued (expires 2026-10-03): http://…/interview/<token>", ... }
      }
    }
  }
}
```

**Stage status values:** `pending` · `success` · `failed` · `skipped` · `awaiting`.

* `failed` — this stage raised an error for this candidate (log-and-skip); reason in `error`.
* `skipped` — stage deliberately not run (e.g. Stage 2 rejected → Stage 3 skipped; no phone number); reason in `note`.
* `awaiting` — waiting on the candidate (interview link issued, or last call rescheduled/abandoned); details in `note`.

Re-running a stage (e.g. `extract --force`) resets every later stage of that candidate to `pending`
(note: `reset: upstream … re-ran`), so downstream results are never stale.

**overall_status:** `failed` if any stage failed; `rejected` if Stage 2 said
rejected; `awaiting` if Stage 3 is waiting on the candidate; `completed` when Stage 3 succeeded;
otherwise `active`.

---

## 4. JSON schemas (stage outputs)

All stage outputs share an envelope:

```json
{
  "schema_version": "1.0",
  "candidate_id": "<uuid4>",
  "stage": "stage1_extraction",
  "run_id": "<uuid4 of the pipeline run>",
  "created_at": "<iso8601 utc>",
  "llm": { "provider": "gemini", "model": "gemini-3.8-flash", "prompt_file": "prompts/..." },
  "...stage-specific body..."
}
```

### 4.1 Stage 1 — `stage1_extracted/<candidate_id>.json`

```json
{ "...envelope...",
  "source_file": "data/input/resumes/jane_doe.docx",
  "source_sha256": "<hex>",
  "resume_char_count": 4123,
  "extraction": {
    "full_name": "Jane Doe",
    "email": "jane@example.com",            // null if absent
    "phone": "+91 98765 43210",             // null if absent
    "location": "Hyderabad, India",
    "linkedin_url": null,
    "headline": "Senior Python Engineer",
    "summary": "…",
    "total_experience_years": 7.5,           // number or null
    "current_title": "Senior Software Engineer",
    "current_company": "Acme Corp",
    "skills": ["Python", "FastAPI", "PostgreSQL"],
    "work_experience": [
      { "title": "…", "company": "…", "location": null,
        "start_date": "2021-03", "end_date": null, "is_current": true,
        "highlights": ["…"] }
    ],
    "education": [
      { "degree": "B.Tech", "field_of_study": "Computer Science",
        "institution": "…", "graduation_year": 2016 }
    ],
    "certifications": ["AWS Certified Developer – Associate"],
    "languages": ["English", "Hindi"]
  }
}
```
Dates: `YYYY-MM` or `YYYY`; `end_date: null` + `is_current: true` for present roles.
Nothing may be invented — unknown ⇒ `null` / `[]`.

### 4.2 Stage 2 — `stage2_shortlist/<candidate_id>.json`

```json
{ "...envelope...",
  "job_id": "python-backend-engineer-001",
  "criteria": [
    { "criterion": "must_have_skills", "score": 90, "weight": 0.40, "evidence": "…" },
    { "criterion": "experience",       "score": 80, "weight": 0.30, "evidence": "…" },
    { "criterion": "nice_to_have_skills","score": 50, "weight": 0.15, "evidence": "…" },
    { "criterion": "role_relevance",   "score": 85, "weight": 0.15, "evidence": "…" }
  ],
  "matched_must_have_skills": ["Python", "SQL"],
  "missing_must_have_skills": ["Docker"],
  "matched_nice_to_have_skills": ["AWS"],
  "overall_score": 80.25,                   // computed in code: Σ score×weight, 2 dp
  "threshold": 70,
  "decision": "shortlisted",                // shortlisted | rejected
  "decision_reasons": ["overall_score 80.25 ≥ threshold 70"],
  "rationale": "…LLM's short narrative…"
}
```
The LLM returns only per-criterion scores (0–100 integers), evidence, skill
lists and rationale. **Weights, the weighted sum and the decision are computed
in Python** so the decision is deterministic and auditable.

### 4.3 Stage 3 — `stage3_calls/<candidate_id>.json`

```json
{ "...envelope...",
  "job_id": "python-backend-engineer-001",
  "call": {
    "agent": "in_house_screening_agent",
    "channel": "browser_voice",               // browser_voice | browser_text | terminal_simulation | local_transcript_file
    "session_id": "<uuid4>",
    "status": "completed",                    // completed | rescheduled | opted_out | abandoned | failed
    "started_at": "…", "ended_at": "…", "duration_seconds": 312.4,
    "agent_turns": 10, "candidate_turns": 9, "questions_asked": 7,
    "reschedule_note": null,                  // e.g. "tomorrow after 5 pm"
    "agent_llm": "gemini/gemini-3.8-flash"
  },
  "transcript_path": "data/stage3_calls/transcripts/<candidate_id>.txt",
  "screening": {
    "interested_in_role": true,               // true | false | null
    "current_location": "Hyderabad",
    "willing_to_relocate": null,
    "notice_period_days": 30,
    "current_ctc": "18 LPA",                  // free text as stated
    "expected_ctc": "24 LPA",
    "available_for_interview": "Weekdays after 5pm",
    "answers": [
      { "question_id": "notice_period", "question": "…", "answered": true, "answer_summary": "…" }
    ],
    "candidate_questions": ["…"],
    "red_flags": ["…"],
    "sentiment": "positive",                  // positive | neutral | negative
    "overall_summary": "…"
  }
}
```

---

## 5. Thresholds & tunables (`config/settings.yaml`)

| Key | Default | Meaning |
|---|---|---|
| `thresholds.min_resume_chars` | 200 | Stage 1 fails (log-and-skip) if extracted text is shorter — scanned/empty docx |
| `thresholds.max_resume_chars` | 60000 | Text beyond this is truncated (logged as WARNING) |
| `thresholds.shortlist_score` | 70 | Stage 2: `overall_score ≥ this` ⇒ shortlisted |
| `thresholds.require_all_must_have` | false | If true, any missing must-have skill ⇒ rejected regardless of score |
| `scoring.weights` | 0.40 / 0.30 / 0.15 / 0.15 | must_have / experience / nice_to_have / role_relevance — must sum to 1.0 (validated at startup) |
| `llm.model` | `gemini-3.8-flash` | Gemini model code |
| `llm.temperature` | 0.0 | Deterministic-as-possible output |
| `llm.timeout_seconds` | 120 | Per request; SDK retries are explicitly disabled (no-retry policy) |
| `stage3.public_base_url` | `http://127.0.0.1:8765` | Base of interview links (use an HTTPS URL for other machines) |
| `stage3.invite_ttl_days` | 7 | Link lifetime |
| `stage3.max_followups_per_question` | 1 | Clarifying follow-ups the agent may ask per question |
| `stage3.max_candidate_turns` | 40 | Hard stop for runaway conversations |
| `stage3.max_turn_chars` | 2000 | Longer candidate messages are truncated |
| `stage3.speech_lang` | `en-IN` | Browser speech recognition / voice language |
| `stage3.agent_temperature` | 0.4 | Variety in spoken replies (parsing stays at `llm.temperature`) |

---

## 6. Failure-handling policy — **log-and-skip**

For every candidate × stage:

1. Wrap the unit of work in `try/except`.
2. On error: **(a)** `logger.error(...)` with `run_id`, `stage`, `candidate_id`,
   source file, error type + message; **(b)** append one line to
   `data/logs/failures.jsonl`; **(c)** set the stage in
   `candidates_index.json` to `failed` with `error` = `"<Type>: <message>"`;
   **(d)** continue with the next candidate.
3. **No retries.** A failed stage is re-attempted only on a later run
   (stages with `failed`/`pending` are picked up again; `success` is skipped
   unless `--force`).
4. Downstream stages only process candidates whose upstream stage is `success`.
   Candidates not eligible are marked `skipped` with a reason (never silently ignored).
5. Every file in the input folder (except Word `~$` lock files and hidden files)
   gets a `candidate_id` at ingest — including corrupt `.docx` and non-`.docx`
   files. Those then fail Stage 1 (`DocxReadError`) with a log line, a
   `failures.jsonl` line and `stage1_extraction.status = failed`, so nothing
   disappears without a trace.
7. Integrity checks that fail a candidate: source file changed since ingest
   (`SourceChangedError`), stage file's `candidate_id` ≠ index key
   (`JoinKeyMismatchError`), LLM output not matching the schema
   (`LLMResponseError`), LLM stopped early (truncated JSON).
6. Config errors (bad YAML, weights ≠ 1, missing API key) abort the run
   **before** any candidate is touched — they are not per-candidate failures.

Failure line (`failures.jsonl`):
```json
{"ts":"…","run_id":"…","stage":"stage1_extraction","candidate_id":"…","source_file":"…","error_type":"LLMResponseError","error":"…"}
```

---

## 7. Code layout (`src/screening/`)

| Module | Responsibility |
|---|---|
| `cli.py` | `screening ingest | extract | shortlist | call | run | serve | simulate-call | status | parse-transcript | check-llm | export-schemas` |
| `config.py` | Load + validate `settings.yaml`, JD, questions, `.env` |
| `paths.py` | Resolve project/data paths |
| `logging_setup.py` | Console + file logging, `failures.jsonl` writer |
| `context.py` | `RunContext` (run_id, config, index, logger) + the single `fail` / `skip` / `block` implementation |
| `index.py` | `CandidateIndex` — load/save (atomic), register, update stage |
| `schemas.py` | Pydantic models for §3/§4 (LLM-output models + stored records) |
| `prompts.py` | Load prompt files, `{{placeholder}}` substitution (fails on unfilled) |
| `docx_reader.py` | `.docx` → plain text (paragraphs, tables, headers/footers, in order) |
| `llm/base.py`, `llm/gemini.py` | Provider-neutral `LLMClient.generate_json(system, prompt, schema)`; Gemini impl |
| `stage0_ingest.py` | Discover resumes, hash, assign UUID4, register |
| `stage1_extract.py` | Stage 1 |
| `stage2_shortlist.py` | Stage 2 |
| `stage3_call.py` | Stage 3: interview links, `finalize_dialogue`, transcript parsing |
| `agent/dialogue.py` | `ScreeningDialogue`: the agent's state machine |
| `agent/invites.py` | Interview-link tokens (`invites.json`) |
| `agent/service.py` | Live sessions for the web server (persist per turn, finalize in background) |
| `dashboard/server.py`, `static/index.html`, `static/interview.html` | `screening serve`: HR dashboard (loopback only) + candidate interview page |

---

## 8. Stage 3 — in-house screening agent

Replaces the former WAPI integration (removed 2026-09-26 at the owner's request).

**Flow.** `screening call` gives each shortlisted candidate a private link
`<stage3.public_base_url>/interview/<token>` (token = `secrets.token_urlsafe(24)`,
expires after `stage3.invite_ttl_days`; one active link per candidate; `--force`
issues a new one and revokes the old). Stage 3 → `awaiting`. HR shares the
link (nothing is emailed automatically). `screening serve` hosts the page.

**The conversation** (`agent/dialogue.py`) is a code-controlled state machine:
`consent → question 1 … N (config order) → close`. Per candidate turn, one LLM
call (`agent_turn.md`, schema `AgentTurnLLM`) classifies the reply
(`answer | consent_yes | question | reschedule | opt_out | unclear | other`),
says whether the current step is resolved, and writes the next spoken line.
Code enforces: questions are never skipped or reordered; at most
`stage3.max_followups_per_question` follow-ups (+1 grace turn if the candidate
asks something), after which code moves on with the next question verbatim;
`max_candidate_turns` hard cap; opt-out / reschedule end immediately; one LLM
failure → "please repeat", two in a row → graceful goodbye (`failed`).
The opening line always discloses AI + recording and asks for consent.

**Channels.** Browser voice (Web Speech API: browser speech-to-text +
text-to-speech; the mic needs a secure origin — localhost or HTTPS), browser
text, and `screening simulate-call` in the terminal. All produce the same record.

**When a call ends** (`finalize_dialogue`): transcript saved → parsed with
`parse_transcript.md` → `stage3_calls/<candidate_id>.json`. Outcomes:

| Outcome | Stage 3 status | Link |
|---|---|---|
| `completed` | `success` | marked used |
| `opted_out` | `skipped` (record kept; never re-invited automatically) | revoked |
| `rescheduled` / `abandoned` / `failed` | `awaiting` (partial record kept) | stays valid |

A session in which the candidate never spoke writes no record. Parse errors
are log-and-skip (the transcript is kept).

**Server** (`dashboard/server.py`): dashboard routes answer loopback clients
only; the interview API `/api/interview/<token>/{info,start,turn,end}` exposes
only first name, job title and company. `Referrer-Policy: no-referrer`, strict
CSP, 16 KB body cap, sessions persisted after every turn.

## 9. Assumptions (made because the source docs were unavailable)

1. Folder names, file names, JSON field names in this document are my own.
2. Stage 2 scores against one job description (`config/job_description.yaml`) per run.
3. Scoring = 4 weighted criteria, threshold 70 (tunable).
4. Stage 3 needs no phone number: candidates join via a browser link that HR shares with them.
5. Stage 3 screening questions (notice period, CTC, location, interest, availability) are a sensible default set, editable in `config/screening_questions.yaml`.
6. Only `.docx` is supported (as stated in the kickoff). `.doc`/`.pdf` in the input folder are registered and then fail Stage 1.
7. Test resumes in `samples/` are synthetic placeholders until the owner provides the real 3.
8. Stage 2 is scored "blind": name, email, phone, LinkedIn and location are removed from the profile sent to the LLM.
9. `job_description.yaml` has a `company_name` (used by the call agent to introduce itself); set to `Kanerika Inc`.
10. The call agent discloses it is an AI and that the call is recorded (consent), and honours opt-out requests.
11. Browser speech recognition in Chrome/Edge is processed by the browser vendor's cloud service; this is disclosed on the landing page, and candidates can type instead.
