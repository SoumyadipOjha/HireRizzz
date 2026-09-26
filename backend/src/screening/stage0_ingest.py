"""Stage 0 — discover resumes, assign candidate_id (UUID4), register in the index.

Every real file in the input folder gets an index row, including unsupported
or corrupt ones: they fail visibly in Stage 1 instead of disappearing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from .context import RunContext


@dataclass
class IngestResult:
    new: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_ignorable(path: Path) -> str | None:
    if path.name.startswith("~$"):
        return "Word lock/temp file"
    if path.name.startswith("."):
        return "hidden file"
    return None


def resumes_dir(ctx: RunContext, job_id: str | None) -> Path:
    """Where a job's resumes are dropped: input/resumes/ for the default job, input/resumes/<job_id>/ otherwise."""
    job_id = ctx.config.jobs.stored_id(job_id)
    root = ctx.config.data.input_resumes
    return root if job_id is None else root / job_id


def ingest(ctx: RunContext, input_dir: Path | None = None, job_id: str | None = None) -> IngestResult:
    """Register the resumes in a job's folder (job_id None = the default job)."""
    job_id = ctx.config.jobs.stored_id(job_id)
    input_dir = Path(input_dir or resumes_dir(ctx, job_id))
    log, index = ctx.logger, ctx.index
    result = IngestResult()

    if not input_dir.is_dir():
        raise FileNotFoundError(f"Input folder does not exist: {input_dir}")

    files = sorted((p for p in input_dir.iterdir() if p.is_file()), key=lambda p: p.name.lower())
    log.info("stage0_ingest: scanning %s (%d files)", input_dir, len(files))

    for path in files:
        if reason := _is_ignorable(path):
            log.debug("stage0_ingest: ignoring %s (%s)", path.name, reason)
            continue

        source = ctx.config.data.rel(path)
        digest = sha256_file(path)

        if existing := index.find_by_sha256(digest, job_id):
            log.info("stage0_ingest: unchanged %s -> candidate_id=%s", path.name, existing.candidate_id)
            result.unchanged.append(existing.candidate_id)
            continue

        if existing := index.find_by_source(source):
            # Same file name, new content: keep the join key, restart the pipeline for it.
            log.warning("stage0_ingest: %s changed on disk; resetting all stages for candidate_id=%s",
                        path.name, existing.candidate_id)
            index.reset(existing.candidate_id, digest)
            result.updated.append(existing.candidate_id)
            continue

        entry = index.register(source, digest, job_id)
        log.info("stage0_ingest: registered %s -> candidate_id=%s", path.name, entry.candidate_id)
        result.new.append(entry.candidate_id)

    log.info("stage0_ingest: done — %d new, %d updated, %d unchanged",
             len(result.new), len(result.updated), len(result.unchanged))
    return result
