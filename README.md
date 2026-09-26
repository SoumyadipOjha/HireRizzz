# Recruiting Screening Pipeline (Stages 1–3)

`.docx` resumes → **Stage 1** structured profile → **Stage 2** shortlist
decision → **Stage 3** screening call + parsed transcript. Every candidate
gets structured JSON at each stage, joined by one UUID4 `candidate_id`.
Stage 4 (HR filter) is out of scope.

| Stage | Status |
|---|---|
| 0 Ingest | ✅ built |
| 1 Resume extraction (Gemini) | ✅ built — needs your `GEMINI_API_KEY` for a live run |
| 2 Shortlisting (Gemini + deterministic scoring) | ✅ built — needs `GEMINI_API_KEY` |
| 3 Screening call (in-house agent: browser voice or text) | ✅ built — needs `GEMINI_API_KEY` |

Design docs: [docs/intent.md](docs/intent.md) · [docs/spec.md](docs/spec.md) (folder layout, JSON schemas, thresholds, failure policy) · [docs/build_instructions.md](docs/build_instructions.md)

## Setup (Windows)

Needs [uv](https://docs.astral.sh/uv/) (installed at `%USERPROFILE%\.local\bin\uv.exe`).

```bash
uv sync
copy .env.example .env      # then put your free key from https://aistudio.google.com/apikey in GEMINI_API_KEY
uv run screening check-llm  # verifies the key and that llm.model exists
```

## Storage (MongoDB by default)

Candidates, stage records, transcripts, interview links and failures are stored in
MongoDB (`storage.backend: mongodb` in `config/settings.yaml`). Set `MONGODB_URI`
and `MONGODB_DB_NAME` in `.env`; without them the local server
`mongodb://localhost:27017` and database `recruiting_screening` are used. Resume
files stay in `data/input/resumes/`; logs stay in `data/logs/`.

The original JSON-file layout is still available: `--storage file` on any command
(or `STORAGE_BACKEND=file`). Tests and the offline demo use it; `tests/test_mongo.py`
runs the pipeline against a real MongoDB in a throwaway database (skipped if none is reachable).

## Run

```bash
# put the .docx resumes in data/input/resumes/, then:
uv run screening run            # ingest + stages 1-3
uv run screening status         # table view of candidates_index.json
```

Individual stages: `ingest`, `extract`, `shortlist`, `call` (each supports
`--force` and `--only <candidate_id>`). Use `--data-dir <folder>` to run
against a separate data folder (e.g. `demo_runs/x`).

Try the pipeline on the synthetic samples without touching `data/`:

```bash
uv run screening --data-dir demo_runs/samples ingest --input-dir samples/resumes
uv run screening --data-dir demo_runs/samples run --input-dir samples/resumes
uv run screening --data-dir demo_runs/samples simulate-call <candidate_id>
```

## Dashboard

Read-only web view of `data/` (candidates, scores, criterion breakdown, call
results, interview links, failures, log). Refreshes every 5 s; localhost only.

```bash
uv run screening serve                         # real data -> http://127.0.0.1:8765
uv run python scripts/make_demo_data.py        # build DEMO data in demo_runs/demo (fake LLM, clearly labelled)
uv run screening --data-dir demo_runs/demo --storage file serve
```

## Where things are

| What | Where |
|---|---|
| Thresholds, weights, model | `config/settings.yaml` |
| Job the candidates are scored against | `config/job_description.yaml` |
| Questions the call agent asks | `config/screening_questions.yaml` |
| Every LLM prompt | `prompts/<stage>/*.md` |
| Outputs | `data/stage1_extracted/`, `data/stage2_shortlist/`, `data/stage3_calls/` — `<candidate_id>.json` |
| Master index | `data/candidates_index.json` |
| Logs / failures | `data/logs/pipeline.log`, `data/logs/failures.jsonl` |
| JSON Schemas | `schemas/*.schema.json` (`uv run screening export-schemas`) |

## Failure policy

Log-and-skip, no retries: a failing candidate gets an ERROR log line, a
`failures.jsonl` line and `status: failed` + error in the index; the batch
continues. Re-running picks up only `pending`/`failed` work. Exit code is 1
if any candidate failed, 2 for configuration errors (nothing processed).

## Stage 3: the screening agent

```bash
uv run screening call                            # private link per shortlisted candidate, emailed with a QR code
uv run screening call --resend                   # email active links again (e.g. after fixing SMTP settings)
uv run screening remind                          # remind candidates who haven't started (the server also does this)
uv run screening serve                           # dashboard + interview pages on http://127.0.0.1:8765
uv run screening simulate-call <candidate_id>    # talk to the agent in the terminal (testing)
uv run screening --email outbox call             # dry run: emails go to data/outbox/*.eml, nothing is sent
```

* **Invite email** (`templates/email/invite.*`): a "Start screening" button and a QR
  code (inline image, so Gmail shows it) for the same personal link, sent to the
  email address in the resume. Links expire after `stage3.invite_ttl_days`. An email
  problem never fails the stage: the link is still issued and the index note says why
  it wasn't emailed. Gmail needs an **app password** in `SMTP_PASS`.
* **Confirm it's you**: before the call, the page emails a 6-digit code to that
  address (`stage3.verify_email`). A correct code unlocks the call in that browser
  tab only, so a forwarded link is useless. Codes expire, attempts are limited, and
  only hashes are stored.
* The candidate opens their link, reads what to expect (AI assistant,
  recorded, ~5 minutes) and chooses **voice** (Chrome/Edge/Safari) or **typing**.
* Code runs the structure (consent → each question in order → close);
  Gemini classifies each reply and writes the next line. Follow-ups and turn
  counts are capped in code, so the conversation can't go off the rails.
* When it ends, the transcript is parsed into `stage3_calls/<candidate_id>.json`.
  Completed → link used. Opted out → link revoked. Rescheduled/abandoned → the
  same link works again.
* **Other machines:** microphones only work on `localhost` or HTTPS. To let
  real candidates in, put the server behind an HTTPS URL (e.g. a tunnel or a
  hosted reverse proxy), set `stage3.public_base_url` to it, and run
  `screening serve --host 0.0.0.0`. The dashboard still only answers on the
  machine itself.

## Approvals and results (the AI suggests, people decide)

```
Stage 2 (AI suggests) -> recruiter approves / overrides -> shortlisted: interview link emailed
                                                         -> rejected:    polite rejection email
Stage 3 call -> Stage 4 (AI scores, suggests) -> hiring manager approves / overrides the final lists
                                              -> send-results: "selected" / "not selected" emails
```

```bash
uv run screening review                                   # who is waiting at each gate, with AI suggestions
uv run screening approve-shortlist --by "Riya" --accept-ai        # or --shortlist ID / --reject ID to override
uv run screening evaluate                                 # Stage 4 (also runs automatically after each call)
uv run screening approve-final --by "Dev (Manager)" --accept-ai   # records the manager's final decisions
uv run screening send-results                             # emails them (never twice)
uv run screening results --csv final.csv                  # the output: final shortlisted and rejected lists
```

Every decision stores what the AI suggested, who decided, when, and whether it was overridden.
Stage 4 scores four competencies from the transcript; each score must be backed by quotes that code
checks word for word against the candidate's own lines (quotes that aren't there are dropped and the
candidate is flagged *needs review*). Weights and the threshold are under `evaluation` in settings.yaml.

## Tests

```bash
uv run pytest
```
Offline (a fake LLM stands in for Gemini): docx parsing edge cases, config
validation, schema conversion, scoring maths, end-to-end Stages 0–3 incl.
failure paths.
