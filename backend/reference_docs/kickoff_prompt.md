You are building a recruiting screening pipeline in Python. I'm attaching
three reference documents — read all three fully before writing any
code:

1. `intent.md` — overall flow and scope
2. `spec.md` — folder structure, JSON schemas, thresholds, failure
   handling policy
3. `build_instructions.md` — the exact ordered build steps to follow

**Build order:** Follow `build_instructions.md` step by step, in the
order given (Step 0 → Step 1 → Step 2 → Step 3). Do not skip ahead or
build multiple steps in parallel. After each step, stop and show me the
checkpoint described in that step's "Checkpoint" section before moving
to the next one.

**Scope:** This build ends at Stage 3 (the calling agent producing
structured JSON output per candidate). The Stage 4 HR filter is out of
scope — do not build it.

**Test data:** I will provide 3 test resumes as `.docx` files. Treat
this as a single batch — no need for concurrency, queuing, or retry
logic. Failure policy throughout is log-and-skip, exactly as specified
in `spec.md` and `build_instructions.md` — never silently drop a
candidate without a log entry and an update to
`candidates_index.json`.

**Before you start Step 1, confirm with me:**
- Which free-tier LLM API to use for resume extraction and shortlisting
  (I have not committed to a provider yet — ask me rather than
  assuming one).

**Before you start Step 3:**
- Stop and tell me clearly that this step is blocked on WAPI's
  request/response contract, which is not yet confirmed. Scaffold the
  file structure and stub functions only (as `build_instructions.md`
  Step 3 describes), and do not attempt a real call integration until
  I give you WAPI's actual API details.

**General rules:**
- Match the folder structure, file names, and JSON schemas in `spec.md`
  exactly — don't improvise field names or restructure folders.
- Every LLM prompt used internally (extraction, scoring, transcript
  parsing) should be its own file under the relevant `prompts/`
  subfolder, not inline in the Python code — as specified in
  `build_instructions.md`.
- Use `candidate_id` (UUID4) as the join key across every stage, exactly
  as described.
- If anything in the three documents is ambiguous or you have to make
  an assumption to proceed, stop and ask me rather than guessing.

Start with Step 0 now and show me the checkpoint before continuing.
