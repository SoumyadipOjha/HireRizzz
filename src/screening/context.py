"""Per-run state shared by all stages, and the single log-and-skip implementation."""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field

from .config import AppConfig
from .index import CandidateIndex
from .logging_setup import failure_line, setup_logging
from .schemas import CandidateEntry, StageName


@dataclass
class StageSummary:
    stage: StageName
    succeeded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    awaiting: list[str] = field(default_factory=list)
    already_done: list[str] = field(default_factory=list)

    def line(self) -> str:
        return (f"{self.stage}: {len(self.succeeded)} succeeded, {len(self.failed)} failed, "
                f"{len(self.skipped)} skipped, {len(self.awaiting)} awaiting candidate, {len(self.already_done)} already done")


@dataclass
class RunContext:
    config: AppConfig
    index: CandidateIndex
    logger: logging.Logger
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # Serialises index changes between the web server's threads (interview finalizers, dashboard approvals).
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    @classmethod
    def create(cls, config: AppConfig) -> "RunContext":
        run_id = str(uuid.uuid4())
        config.data.ensure()
        lc = config.settings.logging
        logger = setup_logging(config.data.pipeline_log, run_id, lc.console_level, lc.file_level)
        index = CandidateIndex(config.store)
        return cls(config=config, index=index, logger=logger, run_id=run_id)

    def fail(self, stage: StageName, entry: CandidateEntry, error: BaseException) -> None:
        """Log-and-skip (spec §6): log line + failures.jsonl + index, then the caller continues."""
        msg = f"{type(error).__name__}: {error}"
        self.logger.error("%s FAILED candidate_id=%s file=%s :: %s", stage, entry.candidate_id, entry.source_file, msg)
        self.logger.debug("traceback for candidate_id=%s", entry.candidate_id, exc_info=error)
        self.config.store.add_failure(failure_line(run_id=self.run_id, stage=stage, candidate_id=entry.candidate_id,
                                                   source_file=entry.source_file, error=error))
        self.index.set_stage(entry.candidate_id, stage, "failed", run_id=self.run_id, error=msg[:2000])

    def skip(self, stage: StageName, entry: CandidateEntry, reason: str) -> None:
        self.logger.info("%s SKIPPED candidate_id=%s :: %s", stage, entry.candidate_id, reason)
        self.index.set_stage(entry.candidate_id, stage, "skipped", run_id=self.run_id, note=reason)

    def awaiting(self, stage: StageName, entry: CandidateEntry, reason: str, output_path: str | None = None) -> None:
        """Waiting on the candidate (e.g. interview link sent, call rescheduled)."""
        self.logger.info("%s AWAITING candidate_id=%s :: %s", stage, entry.candidate_id, reason)
        self.index.set_stage(entry.candidate_id, stage, "awaiting", run_id=self.run_id, note=reason,
                             output_path=output_path)
