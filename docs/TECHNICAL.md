# HireRizz: technical documentation

**HireRizz** is an AI recruiting assistant. It writes job posts, reads and scores resumes, catches
resume fraud, runs a voice screening call with each shortlisted candidate, scores that call with
evidence, and emails every candidate their result. People make both hiring decisions.

| | |
|---|---|
| Live app | https://hire-rizzz.vercel.app |
| API | https://hirerizzz.onrender.com (`GET /api/health` shows the deployed commit) |
| Repository | `backend/` (Python API, Render) · `frontend/` (React + Vite, Vercel) |
| Tests | `cd backend && uv run pytest` (128 tests, offline) |

## Team and ownership

| Module | Owner | Main code |
|---|---|---|
| 1. AI job posting | **Siddhart Joshi** | `jd_writer.py`, `jobs.py`, `PostJobModal.jsx`, `LinkedInModal.jsx`, `Countdown.jsx` |
| 2. AI resume screening & blind scoring | **Ayush Rana** | `stage0_ingest.py`, `resume_reader.py`, `stage1_extract.py`, `stage2_shortlist.py`, `approvals.py` (Gate 1), `ResumeTab.jsx` |
| 3. Fraud firewall | **Soumyadip** | `credibility.py`, `company_check.py`, `cooldown.py`, `CredibilityTab.jsx` |
| 4. AI voice screening & evidence-based evaluation | **Prabhat** | `stage3_call.py`, `agent/`, `stage4_evaluate.py`, `approvals.py` (Gate 2, results), `public/interview.html`, `InterviewTab.jsx` |
| 5. Prototyping, testing, PPT, workflow diagram | **Baljit Singh** | `backend/tests/`, demo tooling, the flow diagram, the sales and business decks |

Backend paths are under `backend/src/screening/`; frontend paths are under `frontend/src/` unless noted.

---

## Contents

1. [Architecture](#1-architecture)
2. [Data model](#2-data-model)
3. [Shared platform](#3-shared-platform): storage, AI client, email, auth, jobs, index
4. [Module 1: AI job posting](#4-module-1-ai-job-posting) (Siddhart Joshi)
5. [Module 2: AI resume screening & blind scoring](#5-module-2-ai-resume-screening--blind-scoring) (Ayush Rana)
6. [Module 3: Fraud firewall](#6-module-3-fraud-firewall) (Soumyadip)
7. [Module 4: AI voice screening & evidence-based evaluation](#7-module-4-ai-voice-screening--evidence-based-evaluation) (Prabhat)
8. [Module 5: Prototyping, testing, PPT, workflow diagram](#8-module-5-prototyping-testing-ppt-workflow-diagram) (Baljit Singh)
9. [Frontend](#9-frontend)
10. [API reference](#10-api-reference)
11. [Configuration](#11-configuration)
12. [Security](#12-security)
13. [Deployment and operations](#13-deployment-and-operations)
14. [Known limits](#14-known-limits)
15. [Responsible AI](#15-responsible-ai)

---

## 1. Architecture

```
Browser (recruiter)  ──►  Vercel: React app (/, /jobs/<id>)          ──HTTPS + CORS──►  Render: Python API
Browser (candidate)  ──►  Vercel: public/interview.html (/interview/<token>)  ─────────►      │
                                                                                            ├─► Google Gemini (AI)
                                                                                            ├─► MongoDB Atlas (data)
                                                                                            ├─► Gmail SMTP (email)
                                                                                            └─► Wikidata (company records)
```

- **Backend:** Python 3.12+, managed with `uv`. The HTTP server is the standard library's
  `ThreadingHTTPServer` (`dashboard/server.py`); no web framework. Data models are Pydantic v2. Long
  work (resume scoring after an upload, finalizing a call) runs on background threads, serialised by
  one `RunContext.lock`.
- **Frontend:** React 19 + Vite 7, no UI library. Charts are hand-built SVG. Fonts (Plus Jakarta Sans,
  Space Grotesk) are bundled with `@fontsource`.
- **Pipeline:** each candidate moves through fixed stages. Every stage writes one JSON record per
  candidate, keyed by a UUID `candidate_id`, and updates the candidate's entry in the index.

| Stage | Name in code | Output record | What happens |
|---|---|---|---|
| 0 | ingest | index entry | file registered, SHA-256 de-duplication |
| 1 | `stage1_extraction` | `stage1_extracted` | AI reads the resume into a profile; fraud checks run |
| 2 | `stage2_shortlisting` | `stage2_shortlist` | AI scores the resume (blind); code decides |
| Gate 1 | review `shortlist` | index | recruiter shortlists or rejects |
| 3 | `stage3_calling` | `stage3_calls` + transcript | invite, AI screening call, transcript parsed |
| 4 | `stage4_evaluation` | `stage4_evaluation` | AI scores the call with quotes; code decides |
| Gate 2 | review `final` | index | hiring manager shortlists or rejects; results emailed |

**Failure policy:** log and skip. A failing candidate gets an error log line, a row in `failures`,
and `status: failed` with the reason. The rest of the batch continues, and re-running picks up only
unfinished work.

---

## 2. Data model

MongoDB database `hirerizz`, one document per item, keyed by `_id`. The same layout also works as JSON
files (`storage.backend: file`), which the tests use.

| Collection | Key | Contents |
|---|---|---|
| `jobs` | `job_id` | `JobRecord`: `job` (JobDescription), `questions`, `status` (open/closed), `deadline`, `created_at`, `posted_by`, `is_default` |
| `candidates` | `candidate_id` | `CandidateEntry` (the index row; below) |
| `stage1_extracted` | `candidate_id` | `Stage1Record`: `extraction` (profile), `truncated`, `llm` |
| `stage2_shortlist` | `candidate_id` | `Stage2Record`: `criteria[]`, `overall_score`, `threshold`, `decision`, matched/missing skills, `rationale` |
| `credibility` | `candidate_id` | `CredibilityRecord`: `flags[]`, `summary`, `linkedin_file`, `linkedin_profile` |
| `stage3_calls` | `candidate_id` | `Stage3Record`: `call` (CallInfo), `transcript_path`, `screening` (parsed facts), `answers_verbatim[]` |
| `transcripts` | `candidate_id` | `{text}`: the call transcript, `Agent:` / `Candidate:` lines |
| `sessions` | `session_id` | the live dialogue state, saved after every turn |
| `invites` | `token` | `Invite`: `candidate_id`, `expires_at`, `status`, email/reminder/verification fields, `interrupted_emails` |
| `stage4_evaluation` | `candidate_id` | `Stage4Record`: `competencies[]`, `interview_score`, `final_score`, `suggested_decision`, `needs_review`, `review_reasons` |
| `failures` | auto | one row per failure: `ts`, `stage`, `candidate_id`, `error_type`, `error` |

**`CandidateEntry`** (`schemas.py`), the index row every screen reads:

| Field | Meaning |
|---|---|
| `candidate_id`, `source_file`, `source_sha256`, `ingested_at`, `updated_at` | identity and file |
| `job_id` | the job applied to (`null` = the default job, i.e. created before multiple jobs existed) |
| `display_name` | name from the resume |
| `stages{stage: {status, decision, score, note, error, output_path, updated_at}}` | status is `pending / running / success / failed / skipped / awaiting` |
| `reviews{shortlist, final}` | `Review`: `decision`, `ai_decision`, `by`, `at`, `note` (an override = decision ≠ ai_decision) |
| `notifications{shortlist, final, credibility, screening}` | `Notification`: `kind`, `status` (sent/outbox/failed/skipped), `to`, `at`, `error` |
| `credibility` | summary counts: `red`, `amber`, `green`, `linkedin` |
| `fraud_blocked`, `fraud_cleared` | stopped by red flags / recruiter clearance (`by`, `at`, `note`) |
| `cooling` | `CoolingPeriod`: `since`, `until`, `previous_candidate_id`, `previous_job_title`, `cleared` |
| `current_stage`, `overall_status` | derived by `index._recompute` |

`overall_status` is one of: `failed`, `fraud`, `cooling`, `selected`, `not_selected`, `rejected`,
`evaluated`, `completed`, `awaiting`, `active`, checked in that order.

**Board columns** (`approvals.board_column`): `applied` · `resume_review` · `interview` (shown as
"Screening") · `final_review` · `selected` (shown as "Shortlisted") · `rejected` · `fraud`.

---

## 3. Shared platform

### 3.1 Storage (`storage/`)
`Store` is an abstract interface with two implementations, `MongoStore` and `FileStore`:

- `put_record` / `get_record` / `ref` / `list_records`: stage records and jobs
- `put_text` / `get_text`: transcripts
- `load_index` / `save_index(changed=[ids])`: only changed candidates are written
- `load_invites` / `save_invites`, `add_failure` / `failures`, `describe()`

References look like `mongodb://collection/key`. The backend is chosen by `storage.backend`, the
`STORAGE_BACKEND` environment variable, or `--storage`.

### 3.2 AI client (`llm/`)
- `GeminiClient.generate_json(system, prompt, schema)`: the Pydantic schema is converted to Gemini's
  `response_schema` (`to_gemini_schema`: inlines `$ref`, turns optional fields into `nullable`, drops
  unsupported keys). The answer is parsed and validated with Pydantic again. Output that stops early
  (for example `MAX_TOKENS`) is rejected.
- **Retries:** only 503 (overloaded) and brief 429 rate limits, after waits of 2 s and then 5 s.
- **Used-up quota:** a 429 that says the quota is exceeded is not retried. The model goes into a cooldown
  for Google's `RetryInfo.retryDelay` (30 s to 1 h; 10 min if Google gives none). The cooldown is shared
  process-wide.
- `FallbackClient` tries `gemini-3-flash-preview`, then `gemini-3.1-flash-lite` → `gemini-3.5-flash` →
  `gemini-3.8-flash`. Models in cooldown go last. `model` records which one actually answered.

### 3.3 Email (`mailer.py`)
- `SmtpMailer` (Gmail, STARTTLS 587 or SSL 465) or `OutboxMailer` (`.eml` files; `EMAIL_MODE=outbox`).
- `render_email(name, to, subject, values, images, raw_html)` renders `templates/email/<name>.html` and
  `.txt`, wrapped in `_layout.html`. `{{placeholders}}` are HTML-escaped unless listed in `raw_html`; a
  missing value raises an error.
- QR codes are inline `cid:` images, because Gmail blocks `data:` URIs.

### 3.4 Dashboard login (`dashboard/auth.py`)
- One account. `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` override the defaults in the file.
- **Token:** `base64url(payload).base64url(HMAC-SHA256)`, where the payload is `{u, exp}` and it's valid
  for 7 days. It's signed with `SESSION_SECRET`, or a key derived from the credentials, so changing the
  password signs everyone out.
- **Checks:** comparisons are constant-time. A wrong password waits 1 s before the reply.
- **Enforcement:** every `/api/*` dashboard route needs `Authorization: Bearer <token>`. Decision
  endpoints overwrite `by` with the signed-in user. `DASHBOARD_AUTH=off` disables the login (tests only).

### 3.5 Jobs (`jobs.py`)
- `JobStore(config)` caches `jobs` and exposes `all`, `get(job_id | None)`, `default_id`, `create`,
  `update`, `set_status`, `stored_id` and `candidate_job(entry)`.
- **Default job:** on first run with an empty `jobs` collection, `config/job_description.yaml` and
  `screening_questions.yaml` are imported as the default job.
- **Per-job settings:** `AppConfig.for_job(job_id)` and `RunContext.for_job` / `for_candidate` give each
  candidate's processing the right job description and questions. Stages look the job up per candidate.

---

## 4. Module 1: AI job posting
**Owner: Siddhart Joshi**

### 4.1 What it does
A manager gets from a two-line brief, or an existing job description file, to a posted job with its
own board and a LinkedIn-ready post, in about five minutes.

### 4.2 Flow
1. **Brief:** `POST /api/jd/draft {brief, company_name}` → `jd_writer.draft_jd`.
   - The brief must be 20–6,000 characters.
   - Prompts: `prompts/jd_writer/system.md` and `draft_jd.md`; the output schema is `JDDraftLLM`.
   - Gemini returns the title, location, experience range, must-have and nice-to-have skills, a
     description, 2 role questions, and `language_notes` (biased wording it changed).
2. **Or upload a file:** `POST /api/jd/upload {name, data(base64), company_name}`
   (`DashboardAPI.draft_from_document`).
   - Accepts `.docx` / `.pdf` (through `resume_reader.read_resume`) or `.txt`, up to 10 MB.
   - The text is sent with `from_document=True`, a 15,000-character limit, and a note telling the model
     to keep the facts and tidy the wording.
   - The draft records `from_document` and `source_file`.
3. **Draft saved:** `<data>/jd_draft.json`, read back with `GET /api/jd/draft`. Nothing is live yet.
4. **Questions:** `merge_questions` keeps the default job's **logistics** questions and puts the 2 new
   **role** questions after the first one. In the form the manager can edit, add
   (`logistics_<n>` ids), remove (at least one must remain) and reorder questions. Empty questions are
   dropped.
5. **Post:** `POST /api/jobs` (alias `/api/jd/approve`) `{job, questions, by, job_id?, deadline}` →
   `approve_jd`.
   - Validated with `JobDescription` and `ScreeningQuestions` (ids match `^[a-z0-9_]+$` and are unique;
     min ≤ max experience).
   - A new job id is `slug(title)-YYYYMMDD`, with `-2`, `-3`… on collision.
   - With `job_id` it updates that job instead. Candidates already scored are not re-scored, and the
     response says how many there were.
6. **Deadline:** optional ISO date (`jobs.parse_deadline`). A bare date means the end of that day.
   Sending `""` clears it; leaving the key out keeps it.
7. **Status:** `POST /api/jobs/<id>/status {status: open|closed}`. A closed job refuses uploads.

### 4.3 LinkedIn post (frontend only, `LinkedInModal.jsx`)
- `linkedinPost(job)` builds plain text, since LinkedIn doesn't render markdown:
  - a 🚀 headline, and an intro from the description's first paragraph (up to 600 characters)
  - 📍 location and 💼 experience, and 🛠️ bullets from `-` / `*` / `•` lines in the description
  - ✅ must-have and ⭐ nice-to-have skills
  - a line on the AI screening process, and 👉 the apply line (`apply_url` if set)
  - hashtags: `#hiring #jobs`, the title, the top 3 skills, city jobs, company
- The text is editable in the dialog, with a counter against LinkedIn's 3,000-character limit. **Copy &
  open LinkedIn** copies the text and opens `linkedin.com/feed/?shareActive=true`.
- It opens automatically after posting (the board URL carries `?share=1`), and from Job details.

### 4.4 Countdown (`Countdown.jsx`)
- Ticks every second.
- Colours: green normally, amber in the last 3 days, red "Deadline passed".
- Shown in compact form on job rows, and in full in the board header and Job details.

### 4.5 Tests
`tests/test_jd_writer.py`: the draft isn't live until approved, a job is posted as a new job, the same
draft posted twice gets a new id, editing, the HTTP flow, JD file upload (.txt / .docx, wrong type
refused), and deadline create, keep, clear and invalid.

---

## 5. Module 2: AI resume screening & blind scoring
**Owner: Ayush Rana**

### 5.1 Stage 0: ingest (`stage0_ingest.py`)
- **Upload:** `POST /api/resumes {job_id, files:[{name, data}]}` takes 1–20 `.docx` / `.pdf` files, up
  to 10 MB each.
- **Where files go:** into `input/resumes/` for the default job, or `input/resumes/<job_id>/` otherwise.
  Names are sanitised, and same-name files with different content get a `_2` suffix.
- **Registering:** `ingest(ctx, job_id)` registers each file with a UUID. The SHA-256 of the content
  de-duplicates within a job (the same file on two jobs = two applications). A changed file with the
  same name resets that candidate's stages.
- **Background run:** after an upload, ingest → stage 1 → stage 2 runs on a background thread, with
  progress in `overview.processing`.

### 5.2 Stage 1: extraction (`resume_reader.py`, `stage1_extract.py`)
- **Reading the file:**
  - `.docx`: paragraphs and tables, through `docx_reader`.
  - `.pdf`: `pypdf`. Password-protected or unreadable files are refused with a clear error.
  - Text under `thresholds.min_resume_chars` (200) fails (for example a scanned image).
  - Text over `max_resume_chars` (60,000) is truncated, and `truncated: true` is set.
- **Extraction:** Gemini (`prompts/stage1_extraction/`) fills `ResumeExtractionLLM`:
  - name, email, phone, location, LinkedIn URL, headline, summary
  - total years of experience, current title and company
  - skills, `work_experience[]` (title, company, dates, `is_current`, highlights)
  - education, certifications, languages
- **After extraction:** the fraud checks (Module 3) and the cooling-period check run.

### 5.3 Stage 2: blind scoring (`stage2_shortlist.py`)
- **Blinding:** `blind_profile` removes `full_name`, `email`, `phone`, `linkedin_url` and `location`
  before scoring.
- **Scoring:** Gemini (`prompts/stage2_shortlisting/`) returns `ShortlistAssessmentLLM`:
  - a 0–100 score with evidence for each criterion
  - matched and missing skills
  - a rationale
- **Matching skills back to the job:** `reconcile_skills` maps the AI's skill names back to the job's
  wording.
  - A skill the job didn't list is ignored, with a warning.
  - A skill listed as both matched and missing counts as missing.
- **Decision (code, `decide`):** `overall = Σ score × weight`.

  | Criterion | Weight |
  |---|---|
  | Must-have skills | 0.40 |
  | Experience vs. the required range | 0.30 |
  | Nice-to-have skills | 0.15 |
  | Role relevance | 0.15 |

  - `overall ≥ thresholds.shortlist_score` (70) → the AI suggests *shortlisted*.
  - With `require_all_must_have: true`, any missing must-have skill → *rejected*.
- **Skipped:** fraud-stopped and cooling-period candidates don't reach this stage.

### 5.4 Gate 1: resume review (`approvals.approve_shortlist`)
- `POST /api/approve/shortlist {decisions:{candidate_id: shortlisted|rejected}}`.
  - Recorded as a `Review` with the AI's suggestion, so overrides are visible.
- **Refused:** fraud-blocked and cooling-period candidates, and resumes not yet scored.
- **Shortlisted:** runs Stage 3 for those candidates, which sends the invite (Module 4).
- **Rejected:** a `resume_rejected` email (if `approvals.email_resume_rejections`).
- **Bulk:** the board's "Accept AI suggestions" sends every card in the column with its AI decision.

### 5.5 Score breakdown (frontend)
- **Overview tab:** each criterion shows `score/100 × weight = points`, adding up to the resume score,
  with the pass mark. The same view covers the screening score and `final = resume × 40% + screening ×
  60%`.
- **Resume tab:** criteria with evidence, ✓ / ✗ / + skill tags, the rationale, and the full profile.

### 5.6 Tests
`tests/test_pipeline.py`, `test_units.py` (skill reconciliation, decision rule, blinding, Gemini retries
and quota), `test_pdf.py`, `test_approvals.py`, and `test_jobs.py` (each candidate scored against their
own job).

---

## 6. Module 3: Fraud firewall
**Owner: Soumyadip**

### 6.1 Resume checks (`credibility.resume_checks`), code only and instant

| Check | Rule | Level |
|---|---|---|
| `experience_inflated` | claimed total years minus months covered by dated full-time roles (overlaps merged) is > 1 year | red if > 2.5 years, else amber |
| `skill_older_than_tech` | "N years of X" in the text, where N > (this year − X's release year) + 0.5; `TECH_RELEASED` table (for example FastAPI 2018, Microsoft Fabric 2023) | red |
| `future_date` | a role starting more than a month in the future | red |
| `end_before_start` | end date before start date | red |
| `timeline_overlap` | two full-time roles overlapping by more than 2 months | amber |
| `work_before_graduation` | a full-time role starting more than a year before the first graduation | amber |
| `missing_dates` | a role without a start date | amber |

Internships are excluded from the timeline rules. Dates are parsed as month indexes (`Jan 2020`,
`2020-01`, `Present`).

### 6.2 Company existence (`company_check.py`)
- **Lookup:** each employer is looked up on Wikidata:
  - `wbsearchentities`, then `wbgetentities`
  - founded P571, dissolved P576, official website P856
  - the website is pinged
- **Rate and caching:** paced to at most 1 request a second, 429s retried, results cached per company.
- **Flags:**
  - *red* `company_founded_after_role` (joined before the company existed) and
    `company_dissolved_before_role`
  - *amber* `company_not_found`, `company_website_down` and `company_lookup_failed`
  - *green* `company_found`
- A company that isn't listed is only ever amber, so small, genuine companies aren't punished.

### 6.3 LinkedIn cross-check (`credibility.linkedin_checks`)
- `POST /api/candidate/<id>/linkedin {name, data}` takes the candidate's LinkedIn "Save to PDF" export.
  Gemini reads it with the Stage 1 prompt, then code compares it with the resume:
  - `linkedin_name`: a different person (red)
  - `linkedin_missing_company`: a resume employer not on LinkedIn (red, or amber for internships)
  - `linkedin_dates`: dates more than 3 months apart (amber; red if more than 12 months, or if "current"
    differs)
  - `linkedin_title` (amber), `linkedin_extra_company` (amber), `linkedin_total_experience` (amber),
    `linkedin_match` (green)

### 6.4 Blocking, email, clearing
- **Blocking:** `index.set_credibility(summary, block_min_red)`. At `credibility.min_red_to_block` (1)
  red flags, `fraud_blocked = true`. Stages 2–4, the gates and the interview page all refuse a blocked
  candidate.
- **Email:** `request_clarification`, sent automatically when `email_candidate_on_fraud: auto`, or with
  the "Email the candidate" button.
  - The `clarification` email lists each red mismatch in candidate-friendly wording
    (`candidate_wording`).
  - It asks them to fix the resume and **reapply through the job posting**, with a *Reapply* button when
    the job has `apply_url`.
- **Clearing:** `POST /api/candidate/<id>/clear-fraud {note}` needs a reason. It records `by`, `at`,
  `note` and `red_at_clearance`, then scores the resume in the background. A *new* red flag later
  blocks the candidate again.

### 6.5 Cooling period (`cooldown.py`)
- **What it is:** once someone *finishes* a screening call, any other application with the same email
  (lower-cased, any job) is held until `since + stage3.cooldown_days` (30).
- **When it's checked:** after Stage 1, and again at Stages 2 and 3, so an earlier application
  finishing its call counts too.
- **While held:** `overall_status: cooling`, the Rejected column, and no scoring, invite or Gate 1.
- **Letting someone through:** `POST /api/candidate/<id>/clear-cooling {note}` ("Allow anyway") needs a
  reason and records the clearance.
- `cooldown_days: 0` turns it off.

### 6.6 Tests
`tests/test_credibility.py`, `test_company_check.py` (with a fake Wikidata), `test_fraud_block.py`
(blocking, clearing, clarification email, reapply button), and `test_cooldown.py`.

---

## 7. Module 4: AI voice screening & evidence-based evaluation
**Owner: Prabhat**

### 7.1 Invites (`stage3_call.run_stage3`, `agent/invites.py`, `agent/notify.py`)
- **Token:** each shortlisted candidate gets an `Invite` with `secrets.token_urlsafe(24)` (32
  characters, about 192 bits). It expires after `stage3.invite_ttl_days` (3) and is single-use once the
  call is completed.
- **Link:** `PUBLIC_BASE_URL/interview/<token>`.
- **Invite email** (`invite` template):
  - a button and an inline **QR code** (`segno`)
  - the expiry date and the expected length (≈ 0.8 min per question, at least 5)
  - the **backup phone number** (`stage3.support_phone`, +1 (463) 215-0098)
- **Reminder:** `send_reminders` runs every 10 minutes in the server. It sends one reminder
  `reminder_after_hours` (24) after the invite, if no call has started.
- **Optional one-time code** before the call (`stage3.verify_email`, currently off): 6 digits, only
  hashes stored, limited attempts, a resend gap, and the correct code unlocks that browser tab only.

### 7.2 The screening page (`frontend/public/interview.html`)
- **Before the call:** plain HTML + JS. It reads what to expect from `/api/interview/<token>/info`: first
  name, role, company, number of questions, minutes.
- **Voice or text:**
  - **Voice** uses the browser's Web Speech API: `SpeechRecognition` (language `stage3.speech_lang`,
    en-IN) for listening and `speechSynthesis` for speaking. There's no paid voice service.
  - **Text chat** is always available.
- **Endpoints:** `start` → `turn` (each candidate utterance) → `end`. Closing the tab sends `end` through
  `navigator.sendBeacon`.
- **Errors:** error views show the backup phone number.

### 7.3 The dialogue engine (`agent/dialogue.py`, `agent/service.py`)
- **Code controls the structure; Gemini only classifies and phrases** (`prompts/stage3_calling/`, temperature 0.4):
  - **Opening:** a fixed template discloses that it's an AI and that the call is recorded, then asks
    for consent.
  - **Consent:** up to 3 attempts, then it ends as *rescheduled*.
  - **Questions:** every question in order.
    - Each turn, Gemini returns `AgentTurnLLM {candidate_intent, current_step_resolved, reschedule_note, say}`.
    - At most `max_followups_per_question` (1) follow-up per question, plus one extra turn for the
      candidate's own question, then progress is forced.
  - **Intents:** `opt_out` ends as *opted_out* (link revoked); `reschedule` ends as *rescheduled*
    (link kept).
  - **Limits:** `max_candidate_turns` (40) and `max_turn_chars` (2,000). Silence repeats the last line.
    Two AI failures in a row end the call as *failed* with a polite message.
- **Service rules:**
  - One live session per link; a reload abandons the old session.
  - Every turn is saved to `sessions`, so a crash loses nothing.
  - Finalizing runs on a background thread.
- **Interrupted calls:**
  - An *abandoned* or *failed* call where the candidate had spoken is an interrupted call.
  - After `interrupted_grace_seconds` (45; a quick reload doesn't count), if there's no live session
    and the link is still active, the candidate gets a `call_halted` email: "Complete my screening",
    the same link, and the phone number.
  - At most `max_interrupted_emails` (3). The email is recorded as `notifications.screening`.
  - Calls idle for `idle_minutes` (10) are closed as interrupted, by a sweeper thread.

### 7.4 Finalizing the call (`stage3_call.finalize_dialogue`)
- **Transcript:** saved to `transcripts`.
- **Parsing:** Gemini (`parse_transcript.md`) → `TranscriptParseLLM`:
  - interested, current location, relocation, notice period (days), current and expected CTC,
    availability
  - one answer per question, the candidate's own questions, red flags, sentiment, a summary
- **Answers:** `reconcile_answers` forces exactly one answer per configured question, in order.
  `build_answers` stores each answer **word for word**, with the full exchange.
- **Outcomes:**
  - *completed* → stage success, link used
  - *opted_out* → stage skipped, link revoked
  - *other* → awaiting, link still works
- A completed call is scored straight away (Stage 4).

### 7.5 Stage 4: evidence-based evaluation (`stage4_evaluate.py`)
- **Scoring:** Gemini (`prompts/stage4_evaluation/`) scores four competencies from 0–100, each with 1–3
  quotes copied from the candidate, plus concerns and a summary.

  | Competency | Weight |
  |---|---|
  | Role knowledge | 0.35 |
  | Problem solving | 0.25 |
  | Communication | 0.20 |
  | Motivation | 0.20 |
- **Quote check (code, `verify_quotes`):** each quote must appear in the candidate's own lines. Quotes
  that don't are **dropped** and listed as `unverified_quotes`.
- **Needs review:** set when a competency with a score above 0 has fewer than `min_verified_quotes` (1)
  verified quotes, when quotes were dropped, or when the call had red flags.
- **Scores:**
  - `interview_score = Σ score × weight`
  - `final_score = resume × 0.4 + interview × 0.6`
  - `final ≥ evaluation.final_threshold` (65) → the AI suggests *shortlisted*

### 7.6 Gate 2 and results (`approvals.py`)
- `POST /api/approve/final {decisions}`: the hiring manager decides and can override the AI. The
  decision can be changed later, and the result emailed again.
- `GET /api/results?job=` returns the final shortlist, rejected and awaiting lists.
  `GET /api/results.csv` exports them.
- `POST /api/send-results {job_id}` emails `selected` / `not_selected` to everyone whose current decision
  hasn't been emailed yet.

### 7.7 Tests
`tests/test_agent.py` (the whole dialogue, over HTTP, hang-up, interrupted-call email with the phone
number, idle close), `test_answers.py`, `test_stage4.py`, `test_approvals.py`, `test_email.py`.

---

## 8. Module 5: Prototyping, testing, PPT, workflow diagram
**Owner: Baljit Singh**

- **Test suite** (`backend/tests/`, 128 tests):
  - `conftest.py` forces the file store, outbox email and no login, and blocks Wikidata calls.
  - `FakeLLM` returns scripted, schema-valid answers per schema: profiles, assessments, JD draft,
    transcript parse, evaluation (including one invented quote that Stage 4 must reject), agent turns.
  - Coverage includes every stage, both gates, emails, fraud rules, company check, cooling period,
    multiple jobs, login, insights, Gemini retries and quota, and the HTTP APIs.
  - `test_mongo.py` runs against a real MongoDB if one is reachable.
- **Prototyping and demo tooling** (`backend/demo_runs/`, git-ignored):
  - `demo_server.py`: a self-contained demo app on :8770 with seeded candidates at every stage and a
    scripted AI.
  - `record_demo.py`: drives the app with Playwright (visible cursor, typing), narrates with neural
    text-to-speech (edge-tts), and mixes the result with ffmpeg into `HireRizz_Demo.mp4`.
  - Test-candidate scripts run real resumes through the live pipeline.
- **Workflow diagram:** the Mermaid flowchart in the root `README.md`, from sign-in to results. It's
  colour-coded AI / rules / human / email / storage.
- **Presentations:** the HireRizz sales deck and business deck:
  - problem, flow, before-vs-after time, the four-slide fraud section
  - team effort vs. HireRizz, monthly and yearly cost, ROI, USPs, trust, future scope
  - figures are marked illustrative

---

## 9. Frontend

```
frontend/
  index.html                     Vite entry
  public/interview.html          the candidate's screening page (plain HTML, not React)
  public/config.js               window.HIRERIZZ_API ("" locally; set at build time on Vercel)
  src/api.js                     every backend call, the session token, 401 → sign-out
  src/lib.js                     board columns, labels, polling, routing, toasts
  src/pages/Login.jsx            split-screen sign-in
  src/pages/JobsPage.jsx         home: Insights + the jobs list
  src/pages/BoardPage.jsx        a job's board: columns, cards, upload, bulk actions
  src/components/Insights.jsx    greeting, KPI tiles, outcomes donut, AI-vs-human gauge, Highlights
  src/components/CandidateDrawer.jsx  side panel: Overview (decisions, score breakdown, progress, emails, invite)
  src/components/ResumeTab.jsx · InterviewTab.jsx · CredibilityTab.jsx
  src/components/PostJobModal.jsx · JobDetailsModal.jsx · LinkedInModal.jsx · ResultsModal.jsx · SystemModal.jsx
  src/components/Charts.jsx · Countdown.jsx · Logo.jsx
  build.mjs                      writes dist/config.js from HIRERIZZ_API_URL
  vercel.json                    build, SPA rewrites (/jobs/*), /interview/:token → interview.html
```

- **Polling:** the board polls `/api/overview?job=` every 5 s. The side panel reloads a candidate when
  their `updated_at` changes. The home page polls `/api/jobs` (10 s) and `/api/insights` (20 s).
- **Routing:** the history API (`/`, `/jobs/<id>`, `?c=<candidate>`, `?share=1`), with no router
  library.
- **Motion:** respects `prefers-reduced-motion`.

---

## 10. API reference

All `/api/*` dashboard routes need `Authorization: Bearer <token>`. `POST` bodies are JSON (the
`Content-Type: application/json` is required), and cross-site origins are refused unless allowed by
`CORS_ORIGINS`. Errors return `{"error": "..."}`: 400 invalid input · 401 not signed in · 404 unknown
job or candidate · 410 link no longer active · 503 AI or email not configured · 502 the AI couldn't
answer.

### Public
| Method | Path | Body → response |
|---|---|---|
| GET | `/api/health` | → `{ok, service, commit}` |
| POST | `/api/login` | `{username, password}` → `{token, user}` |
| GET | `/api/interview/<token>/info` | → `{first_name, job_title, company_name, speech_lang, questions, minutes, verification_required, email_hint}` |
| POST | `/api/interview/<token>/code` · `/verify` | one-time code: send → `{sent_to, expires_in_minutes}`; `{code}` → `{access_key}` |
| POST | `/api/interview/<token>/start` | `{channel: browser_voice\|browser_text, access_key?}` → `{session_id, say, ended}` |
| POST | `/api/interview/<token>/turn` | `{session_id, text}` → `{say, ended, outcome}` |
| POST | `/api/interview/<token>/end` | `{session_id}` → `{ended, outcome}` |

### Dashboard (signed in)
| Method | Path | Body / query → response |
|---|---|---|
| GET | `/api/me` | → `{user, login}` |
| GET | `/api/insights` | → totals, board columns, per-job counts, resume/final scores, AI agreement, calls, fraud checks, hours saved |
| GET | `/api/jobs` | → `{jobs:[{job_id, title, company_name, location, status, deadline, candidate_count, counts{column}, …}], columns}` |
| GET | `/api/jobs/<id>` | → job summary + `job`, `questions`, `candidates[]` (with `board_column`) |
| POST | `/api/jobs` (alias `/api/jd/approve`) | `{job, questions, by, job_id?, deadline?}` → `{job_id, title, questions, updated, already_scored}` |
| POST | `/api/jobs/<id>/status` | `{status: open\|closed}` → `{job_id, status}` |
| GET / POST | `/api/jd/draft` | read the saved draft / `{brief, company_name}` → `{draft}` |
| POST | `/api/jd/upload` | `{name, data, company_name}` → `{draft}` (from a .docx / .pdf / .txt) |
| GET | `/api/overview?job=` | settings, pass marks, processing status, the job's candidates |
| GET | `/api/candidate/<id>` | → `{entry, records{stage}, transcript, answers[], credibility, invite, failures[]}` |
| POST | `/api/resumes` | `{job_id, files:[{name, data}]}` → `{saved[], job_id}` (screening runs in the background) |
| POST | `/api/approve/shortlist` | `{decisions:{id: shortlisted\|rejected}, note?}` → `{recorded, invited[], rejected[], emails{}}` |
| POST | `/api/approve/final` | `{decisions, note?}` → `{recorded, changed[]}` |
| GET | `/api/results?job=` · `/api/results.csv?job=` | final lists · CSV download |
| POST | `/api/send-results` | `{job_id?}` → `{emails{id: status}}` |
| POST | `/api/candidate/<id>/linkedin` | `{name, data}` (PDF) → credibility record |
| POST | `/api/candidate/<id>/clear-fraud` | `{note}` → `{cleared, overall_status}` |
| POST | `/api/candidate/<id>/request-clarification` | → the notification |
| POST | `/api/candidate/<id>/clear-cooling` | `{note}` → `{cleared, overall_status}` |
| GET | `/api/answers?job=` · `/api/failures` · `/api/log?lines=` | screening answers · failures · log tail |

---

## 11. Configuration

`backend/config/settings.yaml` (validated at start; any error stops the server before it touches
candidates):

| Key | Default | Owner |
|---|---|---|
| `thresholds.shortlist_score` · `min_resume_chars` · `max_resume_chars` · `require_all_must_have` | 70 · 200 · 60000 · false | Ayush |
| `scoring.weights` | 0.40 / 0.30 / 0.15 / 0.15 | Ayush |
| `llm.model` · `fallback_models` · `transient_retry_delays` · `timeout_seconds` | see §3.2 · [2, 5] · 120 | shared |
| `stage3.invite_ttl_days` · `max_followups_per_question` · `max_candidate_turns` · `max_turn_chars` | 3 · 1 · 40 · 2000 | Prabhat |
| `stage3.speech_lang` · `agent_temperature` · `verify_email` | en-IN · 0.4 · false | Prabhat |
| `stage3.support_phone` · `email_on_interrupted_call` · `interrupted_grace_seconds` · `max_interrupted_emails` · `idle_minutes` | +1 (463) 215-0098 · true · 45 · 3 · 10 | Prabhat |
| `stage3.cooldown_days` | 30 | Soumyadip |
| `evaluation.resume_weight` · `interview_weight` · `final_threshold` · `competency_weights` · `min_verified_quotes` | 0.4 · 0.6 · 65 · 0.35/0.25/0.20/0.20 · 1 | Prabhat |
| `approvals.require_shortlist_approval` · `email_resume_rejections` | true · true | Ayush |
| `credibility.block_on_fraud` · `min_red_to_block` · `company_check` · `email_candidate_on_fraud` | true · 1 · true · auto | Soumyadip |
| `email.mode` · `reminder_after_hours` · `max_reminders` · `auto_reminders` | smtp · 24 · 1 · true | shared |
| `storage.backend` | mongodb | shared |

**Environment variables:**

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | Gemini access |
| `MONGODB_URI` · `MONGODB_DB_NAME` | database |
| `SMTP_HOST` · `SMTP_PORT` · `SMTP_SECURITY` · `SMTP_USER` · `SMTP_PASS` · `EMAIL_FROM_ADDRESS` · `EMAIL_MODE` | Gmail sending |
| `PUBLIC_BASE_URL` | where interview links point (the Vercel URL) |
| `CORS_ORIGINS` | allowed frontend origins (`*` patterns allowed) |
| `DASHBOARD_PUBLIC` · `DASHBOARD_USERNAME` · `DASHBOARD_PASSWORD` · `SESSION_SECRET` · `DASHBOARD_AUTH` | dashboard access and login |
| `STORAGE_BACKEND` | overrides `storage.backend` |
| `HIRERIZZ_API_URL` | frontend build: the backend URL |
| `RENDER_GIT_COMMIT` | set by Render; shown by `/api/health` |

Secrets live only in `backend/.env` (git-ignored) and in the Render / Vercel settings.

---

## 12. Security

- **Login:** a signed token is checked on every dashboard request, and decisions are recorded under the
  signed-in account. Repeated wrong passwords are slowed.
- **Cross-site requests:** state-changing calls need a JSON content type and an allowed `Origin`, which a
  foreign page can't produce without a CORS preflight the server never grants.
- **Interview links:** unguessable, expiring and single-use. Pages send `Referrer-Policy: no-referrer`,
  and microphone access is allowed only for the page itself.
- **Candidate IDs** are validated as UUIDs, and job ids against a strict pattern. Uploaded files are
  size-limited and type-checked. Stored file names are sanitised.
- **Headers:** Content-Security-Policy `default-src 'self'` (scripts self and inline only),
  `X-Content-Type-Options: nosniff`, `frame-ancestors 'none'`.
- **Blind scoring:** the resume scorer never sees identity fields.
- **Secrets:** read from the environment only; database URIs are masked in logs.

---

## 13. Deployment and operations

- **Backend:** `render.yaml`. The root dir is `backend`, the build is `pip install uv && uv sync --frozen
  --no-dev`, and the start command is `uv run --no-sync screening serve --host 0.0.0.0 --port $PORT
  --no-browser`, with a health check on `/api/health`.
- **Frontend:** Vercel, root dir `frontend`, `npm run build`, which runs `vite build` then `build.mjs`.
- **Deploys:** both redeploy automatically on every push to `main`. If Vercel misses a push, an empty
  commit or **Redeploy** in the Vercel dashboard fixes it.
- **Local run:** `cd frontend && npm install && npm run build`, then `cd backend && uv run screening
  serve`, and open http://127.0.0.1:8765. `npm run dev` gives live reload on :5173.
- **Command line** (`uv run screening <cmd>`): `run`, `ingest`, `extract`, `shortlist`, `call`,
  `evaluate`, `review`, `approve-shortlist`, `approve-final`, `results`, `send-results`, `write-jd`,
  `approve-jd`, `check-credibility`, `simulate-call`, `remind`, `status`, `check-llm`,
  `export-schemas`. The command line works on the default job.

---

## 14. Known limits

- **Gemini free tier:** about 20 requests per model per day. The fallback chain stretches this; heavy use
  needs a paid key.
- **Render free tier:** it sleeps after 15 idle minutes, so the first request after a pause can take up
  to a minute.
- **Uploaded files:** resume files sit on Render's disk, which is wiped on redeploy. Extracted data is
  safe in MongoDB.
- **Voice:** voice screening needs Chrome, Edge or Safari; other browsers fall back to text chat. The
  screening call can't prove identity; the optional one-time code helps.
- **Fraud checks:** they catch inconsistent or impossible claims, not a well-built fake identity. Verify
  employment before an offer.
- **Scale:** the index is loaded into memory and background work uses threads. A job queue and indexed
  queries are the path to thousands of applicants.

---

## 15. Responsible AI

How HireRizz meets each Responsible AI theme, with the gap still open for each.

| # | Theme | How HireRizz meets it | Open gap → next step |
|---|---|---|---|
| 1 | **Fairness** | Blind resume scoring (no name, contact details or location). The same fixed weighted criteria for every candidate. Pass marks applied by code, not the AI. The JD writer removes biased wording. Interview scores count only verbatim quotes. Fraud flags never reject anyone automatically. | Indirect signals (college, career gaps) are still visible, and there's no bias audit → compare pass rates across groups at each gate |
| 2 | **Reliability and Safety** | Schema-validated AI output; truncated or invalid output is rejected. Automatic fallback across 4 models, skipping ones out of quota. One failure never stops the batch. The call is guard-railed (fixed questions, at most 1 follow-up, 40-turn cap, safe exit on AI errors). Every turn is saved; interrupted calls are emailed a resume link; idle calls close after 10 min; a backup phone line covers outages. Settings are checked at start-up. 128 automated tests. | No accuracy measured on real candidates → a labelled benchmark re-run on every change |
| 3 | **Privacy and Security** | Signed-token login on every request, with slowed responses to wrong passwords. Unguessable, expiring, single-use candidate links. No referrer leaks; microphone limited to the screening page. Identity fields removed before scoring. Secrets only in the environment, masked in logs. Cross-site and upload protection. Data stays in the client's own MongoDB. | No consent screen, retention period or data deletion; no identity check on the call → consent and retention for DPDP / GDPR, and the emailed one-time code switched back on |
| 4 | **Inclusiveness** | Voice *or* text screening. 24/7 on any device, no app or account, QR link. Indian English (en-IN) speech. A backup phone line and a resume-your-call email. Consent, "later" and "stop" handled. Every candidate gets a result email. Reduced-motion respected. | Voice needs Chrome, Edge or Safari; English only → more languages and screening by phone call (future scope) |
| 5 | **Transparency** | The call opens by disclosing it's an AI and that it's recorded. Score breakdown (score × weight = points) for the resume, screening and final score. Evidence and exact quotes shown, including dropped ones. Specific, sourced fraud flags. AI suggestions and "needs review" reasons clearly labelled. The model used is recorded. Rules documented here and in the flow diagram. | Candidates don't see their own score breakdown → an "explain my result" view |
| 6 | **Accountability** | Two human gates (recruiter, hiring manager); the AI only suggests. Every decision records who, when, and the AI's suggestion, so overrides are visible. The server sets the signed-in name, so it can't be faked. Clearing fraud or lifting a cooling period needs a written reason. Stage outputs and failures are logged, and each module has a named owner. | One shared admin account → individual accounts with roles |
