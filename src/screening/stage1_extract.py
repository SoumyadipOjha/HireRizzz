"""Stage 1 — resume (.docx / .pdf) -> structured profile JSON via the LLM."""

from __future__ import annotations

from datetime import date

from .context import RunContext, StageSummary
from .resume_reader import read_resume
from .llm import LLMClient
from .paths import resolve_stored
from .prompts import load_prompt
from .schemas import LLMInfo, ResumeExtractionLLM, Stage1Record
from .stage0_ingest import sha256_file

STAGE = "stage1_extraction"
PROMPT_DIR = "stage1_extraction"


class ResumeTooShortError(Exception):
    """Too little text to be a real resume (empty, scanned image, etc.)."""


class SourceChangedError(Exception):
    """Source file differs from what was ingested."""


def run_stage1(ctx: RunContext, llm: LLMClient, *, force: bool = False,
               only: set[str] | None = None) -> StageSummary:
    summary = StageSummary(STAGE)
    system = load_prompt(PROMPT_DIR, "system.md")
    template = load_prompt(PROMPT_DIR, "extract_resume.md")
    th = ctx.config.settings.thresholds
    log = ctx.logger

    for entry in ctx.index.all():
        cid = entry.candidate_id
        if only and cid not in only:
            continue
        if entry.stages[STAGE].status == "success" and not force:
            summary.already_done.append(cid)
            continue

        log.info("%s: candidate_id=%s file=%s", STAGE, cid, entry.source_file)
        try:
            path = resolve_stored(entry.source_file)
            if not path.is_file():
                raise FileNotFoundError(f"source file no longer exists: {entry.source_file}")
            if entry.source_sha256 and sha256_file(path) != entry.source_sha256:
                raise SourceChangedError("file changed since ingest; run `screening ingest` again")

            text = read_resume(path)
            if len(text) < th.min_resume_chars:
                raise ResumeTooShortError(
                    f"only {len(text)} characters of text extracted (min {th.min_resume_chars}); "
                    "empty, scanned or image-only resume?")
            truncated = len(text) > th.max_resume_chars
            if truncated:
                log.warning("%s: candidate_id=%s text truncated from %d to %d chars",
                            STAGE, cid, len(text), th.max_resume_chars)
                text = text[: th.max_resume_chars]

            prompt = template.render(resume_text=text, today=date.today().isoformat())
            extraction = llm.generate_json(system=system.text, prompt=prompt, schema=ResumeExtractionLLM)

            if not extraction.full_name:
                log.warning("%s: candidate_id=%s no candidate name found in resume", STAGE, cid)
            if not extraction.email:
                log.warning("%s: candidate_id=%s no email address found (the interview invite can't be emailed)",
                            STAGE, cid)

            record = Stage1Record(
                candidate_id=cid,
                run_id=ctx.run_id,
                llm=LLMInfo(provider=llm.provider, model=llm.model, prompt_file=template.rel_path),
                source_file=entry.source_file,
                source_sha256=entry.source_sha256 or sha256_file(path),
                resume_char_count=len(text),
                truncated=truncated,
                extraction=extraction,
            )
            ref = ctx.config.store.put_record("stage1_extracted", cid, record.model_dump(mode="json"))
            ctx.index.set_stage(cid, STAGE, "success", run_id=ctx.run_id,
                                output_path=ref, display_name=extraction.full_name)
            log.info("%s: candidate_id=%s OK name=%r skills=%d roles=%d", STAGE, cid, extraction.full_name,
                     len(extraction.skills), len(extraction.work_experience))
            summary.succeeded.append(cid)
        except Exception as e:  # log-and-skip: one bad resume never stops the batch
            ctx.fail(STAGE, entry, e)
            summary.failed.append(cid)

    log.info(summary.line())
    return summary
