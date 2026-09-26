"""The candidates index — the master join table keyed by candidate_id (spec §3).
Stored as candidates_index.json or as the MongoDB `candidates` collection."""

from __future__ import annotations

import uuid

from pydantic import ValidationError

from .storage import Store, write_json_atomic  # noqa: F401  (write_json_atomic re-exported for callers)
from .schemas import (
    STAGES,
    CandidateEntry,
    CandidatesIndexDoc,
    CredibilitySummary,
    Notification,
    Review,
    ReviewGate,
    StageName,
    StageState,
    StageStatus,
    utc_now,
)


class CandidateIndexError(Exception):
    """candidates_index.json is unreadable or invalid."""


class CandidateIndex:
    def __init__(self, store: Store):
        self.store = store
        self.doc = self._load()

    def _load(self) -> CandidatesIndexDoc:
        try:
            raw = self.store.load_index()
            return CandidatesIndexDoc() if raw is None else CandidatesIndexDoc.model_validate(raw)
        except (ValidationError, ValueError) as e:
            # Never overwrite a corrupt index: that would silently lose candidates.
            raise CandidateIndexError(f"candidate index in {self.store.describe()} is invalid; "
                                      f"fix or move it aside before re-running:\n{e}") from e

    def reload(self) -> "CandidateIndex":
        """Re-read from storage (the server is long-lived; the CLI may have changed the index meanwhile)."""
        self.doc = self._load()
        return self

    def save(self, changed: str | None = None) -> None:
        """Persist; `changed` = the one candidate_id that changed (lets MongoDB write a single document)."""
        self.doc.updated_at = utc_now()
        self.store.save_index(self.doc.model_dump(mode="json"), [changed] if changed else None)

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
        self.save(cid)
        return entry

    def reset(self, candidate_id: str, sha256: str) -> CandidateEntry:
        """Source file content changed: keep candidate_id, clear all stage state."""
        entry = self.get(candidate_id)
        entry.source_sha256 = sha256
        entry.stages = {s: StageState() for s in STAGES}
        entry.display_name = None
        entry.updated_at = utc_now()
        _recompute(entry)
        self.save(candidate_id)
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
        changed = [stage]
        for later in STAGES[STAGES.index(stage) + 1:]:
            if entry.stages[later].status != "pending":
                entry.stages[later] = StageState(updated_at=now, run_id=run_id,
                                                 note=f"reset: upstream {stage} re-ran")
                changed.append(later)
        # ...and so are approvals based on it (notifications already sent stay as history).
        if "stage2_shortlisting" in changed or stage == "stage1_extraction":
            entry.reviews.pop("shortlist", None)
            entry.reviews.pop("final", None)
        elif "stage4_evaluation" in changed:
            entry.reviews.pop("final", None)
        if display_name:
            entry.display_name = display_name
        entry.updated_at = now
        _recompute(entry)
        self.save(candidate_id)
        return entry

    def set_review(self, candidate_id: str, gate: ReviewGate, review: Review) -> CandidateEntry:
        entry = self.get(candidate_id)
        entry.reviews[gate] = review
        if gate == "shortlist":
            entry.reviews.pop("final", None)
        entry.updated_at = utc_now()
        _recompute(entry)
        self.save(candidate_id)
        return entry

    def set_credibility(self, candidate_id: str, summary: CredibilitySummary) -> CandidateEntry:
        entry = self.get(candidate_id)
        entry.credibility = summary
        entry.updated_at = utc_now()
        self.save(candidate_id)
        return entry

    def set_notification(self, candidate_id: str, gate: ReviewGate, notification: Notification) -> CandidateEntry:
        entry = self.get(candidate_id)
        entry.notifications[gate] = notification
        entry.updated_at = utc_now()
        self.save(candidate_id)
        return entry


def resume_decision(entry: CandidateEntry) -> str | None:
    """The recruiter's shortlist decision if made, else the AI's Stage 2 suggestion."""
    review = entry.reviews.get("shortlist")
    return review.decision if review else entry.stages["stage2_shortlisting"].decision


def _recompute(entry: CandidateEntry) -> None:
    """Derive current_stage / overall_status from stage states and approvals (spec §3)."""
    st = entry.stages
    statuses = [st[s].status for s in STAGES]

    entry.current_stage = next((s for s in STAGES if st[s].status != "success"), STAGES[-1])

    final = entry.reviews.get("final")
    if "failed" in statuses:
        entry.overall_status = "failed"
    elif final is not None:
        entry.overall_status = "selected" if final.decision == "shortlisted" else "not_selected"
    elif resume_decision(entry) == "rejected":
        entry.overall_status = "rejected"
    elif st["stage4_evaluation"].status == "success":
        entry.overall_status = "evaluated"
    elif st["stage3_calling"].status == "success":
        entry.overall_status = "completed"
    elif st["stage3_calling"].status == "awaiting":
        entry.overall_status = "awaiting"
    else:
        entry.overall_status = "active"
