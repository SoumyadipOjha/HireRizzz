"""JSON-file backend: the original layout of the data directory (spec §2)."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..paths import DataPaths, resolve_stored
from .base import Collection, Store, TextCollection, parse_mongo_ref


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


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class FileStore(Store):
    backend = "file"

    def __init__(self, data: DataPaths):
        self.data = data

    def _dir(self, collection: str) -> Path:
        dirs = {
            "stage1_extracted": self.data.stage1_output,
            "stage2_shortlist": self.data.stage2_output,
            "stage3_calls": self.data.stage3_output,
            "stage4_evaluation": self.data.stage4_output,
            "credibility": self.data.credibility_output,
            "sessions": self.data.stage3_sessions,
            "transcripts": self.data.stage3_transcripts,
        }
        try:
            return dirs[collection]
        except KeyError:
            raise ValueError(f"unknown collection {collection!r}") from None

    # -- index
    def load_index(self) -> dict | None:
        p = self.data.candidates_index
        if not p.exists():
            return None
        # Let the caller see invalid JSON as an error instead of an empty index.
        return json.loads(p.read_text(encoding="utf-8"))

    def save_index(self, doc: dict, changed: list[str] | None = None) -> None:
        write_json_atomic(self.data.candidates_index, doc)

    # -- records
    def put_record(self, collection: Collection, key: str, doc: dict) -> str:
        path = self._dir(collection) / f"{key}.json"
        write_json_atomic(path, doc)
        return self.data.rel(path)

    def ref(self, collection: Collection, key: str) -> str:
        return self.data.rel(self._dir(collection) / f"{key}.json")

    def get_record(self, ref: str) -> dict | None:
        if parse_mongo_ref(ref):
            return None  # written by the MongoDB backend; not readable from files
        return _read_json(resolve_stored(ref))

    def put_text(self, collection: TextCollection, key: str, text: str) -> str:
        path = self._dir(collection) / f"{key}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return self.data.rel(path)

    def get_text(self, ref: str) -> str | None:
        if parse_mongo_ref(ref):
            return None
        try:
            return resolve_stored(ref).read_text(encoding="utf-8")
        except OSError:
            return None

    # -- invites
    def load_invites(self) -> dict[str, dict]:
        p = self.data.stage3_invites
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8")).get("invites", {})

    def save_invites(self, invites: dict[str, dict], changed: list[str]) -> None:
        write_json_atomic(self.data.stage3_invites, {
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "invites": invites})

    # -- failures
    def add_failure(self, line: dict) -> None:
        p = self.data.failures_log
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def failures(self) -> list[dict]:
        p = self.data.failures_log
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def describe(self) -> str:
        return f"file ({self.data.root.as_posix()})"
