"""Logging: console + data/logs/pipeline.log, plus the failures.jsonl writer."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

LOGGER_NAME = "screening"
_FMT = "%(asctime)s %(levelname)-7s [run=%(run_id)s] %(message)s"


class _RunIdFilter(logging.Filter):
    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id[:8]
        return True


def setup_logging(log_file: Path, run_id: str, console_level: str = "INFO", file_level: str = "DEBUG") -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()

    log_file.parent.mkdir(parents=True, exist_ok=True)
    run_filter = _RunIdFilter(run_id)
    formatter = logging.Formatter(_FMT)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level.upper())
    file = logging.FileHandler(log_file, encoding="utf-8")
    file.setLevel(file_level.upper())
    for h in (console, file):
        h.setFormatter(formatter)
        h.addFilter(run_filter)
        logger.addHandler(h)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def append_failure(failures_file: Path, *, run_id: str, stage: str, candidate_id: str,
                   source_file: str | None, error: BaseException) -> None:
    """One JSON line per failed/skipped candidate-stage (spec §6)."""
    failures_file.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": run_id,
        "stage": stage,
        "candidate_id": candidate_id,
        "source_file": source_file,
        "error_type": type(error).__name__,
        "error": str(error),
    }
    with failures_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
