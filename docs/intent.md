# intent.md — What this pipeline is for

> Derived from `reference_docs/kickoff_prompt.md` (original intent doc unavailable).

## Goal
Take a batch of candidate resumes (`.docx`) and automatically move each one
through first-round screening, producing **structured, auditable JSON per
candidate** at every step, keyed by a single `candidate_id`.

## Flow
```
 .docx resumes
      │  Stage 0  ingest      → candidate_id (UUID4) + row in candidates_index.json
      ▼
 Stage 1  Resume extraction   (LLM)  → structured profile JSON
      ▼
 Stage 2  Shortlisting        (LLM scores criteria, code applies weights + threshold)
      │         ├── rejected   → stop (recorded)
      ▼         └── shortlisted
 Stage 3  Screening call      (private link → in-house agent, browser voice/text →
                               transcript → LLM parses → JSON)
      ▼
 [Stage 4  HR filter — OUT OF SCOPE]
```

## Principles
* **One join key.** `candidate_id` links index, stage files, logs and failures.
* **Nothing disappears.** Log-and-skip: every failure is logged, written to
  `failures.jsonl`, and recorded in `candidates_index.json`.
* **Prompts are data.** Every LLM prompt lives in `prompts/<stage>/`, never inline.
* **LLM judges, code decides.** The LLM extracts and scores; thresholds and
  the final shortlist decision are deterministic Python.
* **Simple.** Single batch, sequential, no concurrency/queue/retries.
* **Swappable provider.** Gemini free tier today, behind a small interface.

## Non-goals
Stage 4 HR filter; concurrency; retry/backoff; non-`.docx` formats;
phone-network calling (candidates join by browser link instead).
