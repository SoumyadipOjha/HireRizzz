"""Storage interface. Every stage, the agent and the dashboard persist through this,
so the pipeline runs the same on JSON files or MongoDB.

A *ref* is the string a caller stores in the index (``output_path``,
``transcript_path``) to find a document again. File refs are project-relative
paths (unchanged from the file-only design); MongoDB refs look like
``mongodb://<collection>/<key>``. Every backend can read both kinds, so
records written before a switch stay readable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

# Logical collections. The file backend maps each to a folder in the data dir.
Collection = Literal["stage1_extracted", "stage2_shortlist", "stage3_calls", "stage4_evaluation", "sessions",
                     "credibility"]
TextCollection = Literal["transcripts"]

MONGO_PREFIX = "mongodb://"


class StoreError(Exception):
    """The storage backend is unreachable or misconfigured."""


def mongo_ref(collection: str, key: str) -> str:
    return f"{MONGO_PREFIX}{collection}/{key}"


def parse_mongo_ref(ref: str) -> tuple[str, str] | None:
    if not ref.startswith(MONGO_PREFIX):
        return None
    collection, _, key = ref[len(MONGO_PREFIX):].partition("/")
    return (collection, key) if collection and key else None


class Store(ABC):
    backend: str

    # -- candidates index (spec §3) -------------------------------------------
    @abstractmethod
    def load_index(self) -> dict | None:
        """The whole index as a CandidatesIndexDoc-shaped dict, or None if nothing is stored yet."""

    @abstractmethod
    def save_index(self, doc: dict, changed: list[str] | None = None) -> None:
        """Persist the index. `changed` = candidate_ids that changed (None = all)."""

    # -- JSON records: stage outputs, sessions ---------------------------------
    @abstractmethod
    def put_record(self, collection: Collection, key: str, doc: dict) -> str:
        """Write (replace) one document; returns its ref."""

    @abstractmethod
    def ref(self, collection: Collection, key: str) -> str:
        """The ref put_record(collection, key, ...) returns / would return."""

    @abstractmethod
    def get_record(self, ref: str) -> dict | None:
        """Read a document by ref; None if missing or unreadable."""

    # -- text blobs: call transcripts ------------------------------------------
    @abstractmethod
    def put_text(self, collection: TextCollection, key: str, text: str) -> str: ...

    @abstractmethod
    def get_text(self, ref: str) -> str | None: ...

    # -- interview invites ------------------------------------------------------
    @abstractmethod
    def load_invites(self) -> dict[str, dict]:
        """token -> invite dict."""

    @abstractmethod
    def save_invites(self, invites: dict[str, dict], changed: list[str]) -> None:
        """Persist invites. `changed` = tokens created or modified."""

    # -- failures log (spec §6) ---------------------------------------------------
    @abstractmethod
    def add_failure(self, line: dict) -> None: ...

    @abstractmethod
    def failures(self) -> list[dict]:
        """Oldest first."""

    def describe(self) -> str:
        return self.backend
