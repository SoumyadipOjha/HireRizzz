"""A copy of each candidate's original resume in the store, so the dashboard can show the file
the candidate applied with even when this server's disk doesn't have it (e.g. a host whose disk
is wiped on every deploy, or a resume added from another machine)."""

from __future__ import annotations

import base64
from pathlib import Path

from .schemas import utc_now

CONTENT_TYPES = {".pdf": "application/pdf",
                 ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
MAX_COPY_BYTES = 8 * 1024 * 1024   # larger files keep only their text (a MongoDB document is at most 16 MB)


def save_copy(store, candidate_id: str, path: Path, text: str | None) -> None:
    raw = Path(path).read_bytes()
    store.put_record("resume_files", candidate_id, {
        "name": Path(path).name,
        "content_type": CONTENT_TYPES.get(Path(path).suffix.lower(), "application/octet-stream"),
        "size": len(raw),
        "data": base64.b64encode(raw).decode("ascii") if len(raw) <= MAX_COPY_BYTES else None,
        "text": text,
        "saved_at": utc_now(),
    })


def load(store, entry) -> dict | None:
    """{name, content_type, size, data (base64 or None), text} of the resume, or None if it's gone."""
    from .paths import resolve_stored
    from .resume_reader import read_resume

    try:
        path = resolve_stored(entry.source_file)
        if path.is_file():
            raw = path.read_bytes()
            try:
                text = read_resume(path)
            except Exception:
                text = None
            return {"name": path.name, "content_type": CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"),
                    "size": len(raw), "data": base64.b64encode(raw).decode("ascii"), "text": text}
    except (OSError, TypeError, ValueError):
        pass
    doc = store.get_record(store.ref("resume_files", entry.candidate_id))
    if not doc:
        return None
    return {k: doc.get(k) for k in ("name", "content_type", "size", "data", "text")}


def stored_text(store, candidate_id: str) -> str | None:
    doc = store.get_record(store.ref("resume_files", candidate_id))
    return (doc or {}).get("text")
