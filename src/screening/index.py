"""candidates_index.json — the master join table keyed by candidate_id (spec §3)."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path

from pydantic import ValidationError

from .schemas import (
    STAGES,
    CandidateEntry,
    CandidatesIndexDoc,
    StageName,
    StageState,
    StageStatus,
    utc_now,
)


class CandidateIndexError(Exception):
    """candidates_index.json is unreadable or invalid."""


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class CandidateIndex:
    def __init__(self, path: Path):
        self.path = path
        self.doc = self._load()

    def _load(self) -> CandidatesIndexDoc:
        if not self.path.exists():
            return CandidatesIndexDoc()
        try:
            return CandidatesIndexDoc.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (ValidationError, ValueError) as e:
            # Never overwrite a corrupt index: that would silently lose candidates.
            raise CandidateIndexError(f"{self.path} is invalid; fix or move it aside before re-running:\n{e}") from e

    def reload(self) -> "CandidateIndex":
        """Re-read from disk (the server is long-lived; the CLI may have changed the index meanwhile)."""
        self.doc = self._load()
        return self

    def save(self) -> None:
        self.doc.updated_at = utc_now()
        write_json_atomic(self.path, self.doc.model_dump(mode="json"))

    # -- queries -----------------------------------------------------------

    def get(self, candidate_id: str) -> CandidateEntry:
        try:
            return self.doc.candidates[candidate_id]
        except KeyError:
            raise KeyError(f"Unknown candidate_id {candidate_id}") from None

    def all(self) -> list[CandidateEntry]:
        return sorted(self.doc.candidates.values(), key=lambda c: (c.ingested_at, c.source_file))

    def find_by_sha256(self, sha256: str) -> CandidateEntry | None:
        return next((c for c in self.doc.candidates.values() if c.source_sha256 == sha256), None)

    def find_by_source(self, source_file: str) -> CandidateEntry | None:
        return next((c for c in self.doc.candidates.values() if c.source_file == source_file), None)

    # -- mutations (each one saves immediately) ------------------------------

    def register(self, source_file: str, sha256: str | None) -> CandidateEntry:
        now = utc_now()
        cid = str(uuid.uuid4())
        entry = CandidateEntry(candidate_id=cid, source_file=source_file, source_sha256=sha256,
                               ingested_at=now, updated_at=now)
        self.doc.candidates[cid] = entry
        self.save()
        return entry

    def reset(self, candidate_id: str, sha256: str) -> CandidateEntry:
        """Source file content changed: keep candidate_id, clear all stage state."""
        entry = self.get(candidate_id)
        entry.source_sha256 = sha256
        entry.stages = {s: StageState() for s in STAGES}
        entry.display_name = None
        entry.updated_at = utc_now()
        _recompute(entry)
        self.save()
        return entry

    def set_stage(self, candidate_id: str, stage: StageName, status: StageStatus, *, run_id: str,
                  output_path: str | None = None, error: str | None = None, note: str | None = None,
                  decision: str | None = None, score: float | None = None,
                  display_name: str | None = None) -> CandidateEntry:
        entry = self.get(candidate_id)
        now = utc_now()
        entry.stages[stage] = StageState(status=status, output_path=output_path, updated_at=now, run_id=run_id,
                                         error=error, note=note, decision=decision, score=score)
        # Any new result for a stage makes everything downstream of it stale.
        for later in STAGES[STAGES.index(stage) + 1:]:
            if entry.stages[later].status != "pending":
                entry.stages[later] = StageState(updated_at=now, run_id=run_id,
                                                 note=f"reset: upstream {stage} re-ran")
        if display_name:
            entry.display_name = display_name
        entry.updated_at = now
        _recompute(entry)
        self.save()
        return entry


def _recompute(entry: CandidateEntry) -> None:
    """Derive current_stage / overall_status from stage states (spec §3)."""
    st = entry.stages
    statuses = [st[s].status for s in STAGES]

    entry.current_stage = next((s for s in STAGES if st[s].status != "success"), STAGES[-1])

    if "failed" in statuses:
        entry.overall_status = "failed"
    elif st["stage2_shortlisting"].decision == "rejected":
        entry.overall_status = "rejected"
    elif st["stage3_calling"].status == "success":
        entry.overall_status = "completed"
    elif st["stage3_calling"].status == "awaiting":
        entry.overall_status = "awaiting"
    else:
        entry.overall_status = "active"
