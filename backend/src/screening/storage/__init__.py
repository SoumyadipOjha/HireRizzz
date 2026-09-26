"""Pluggable persistence: JSON files or MongoDB (config `storage.backend`)."""

from typing import TypeVar

from pydantic import BaseModel

from .base import Store, StoreError, parse_mongo_ref
from .file_store import FileStore, write_json_atomic

__all__ = ["Store", "StoreError", "FileStore", "RecordNotFoundError", "load_model", "write_json_atomic",
           "parse_mongo_ref"]

M = TypeVar("M", bound=BaseModel)


class RecordNotFoundError(FileNotFoundError):
    """A ref stored in the index points at nothing."""


def load_model(store: Store, ref: str | None, model: type[M]) -> M:
    """Read the record at `ref` and validate it as `model`."""
    doc = store.get_record(ref) if ref else None
    if doc is None:
        raise RecordNotFoundError(f"record not found: {ref}")
    return model.model_validate(doc)
