"""MongoDB backend. One document per candidate / record / invite, keyed by `_id`.

Collections: candidates, invites, failures, transcripts, and one per record
collection (stage1_extracted, stage2_shortlist, stage3_calls, stage4_evaluation,
sessions). Resume files themselves stay in the input folder; only data lives here.
"""

from __future__ import annotations

import json

from pymongo import MongoClient, ReplaceOne
from pymongo.errors import PyMongoError

from ..paths import resolve_stored
from .base import Collection, Store, StoreError, TextCollection, mongo_ref, parse_mongo_ref

_SCHEMA_VERSION = "1.0"


def _strip(doc: dict | None) -> dict | None:
    if doc is None:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


class MongoStore(Store):
    backend = "mongodb"

    def __init__(self, uri: str, database: str, *, timeout_ms: int = 5000):
        self.uri = uri
        self.database = database
        self._client = MongoClient(uri, serverSelectionTimeoutMS=timeout_ms, appname="recruiting-screening")
        self._checked = False

    @property
    def db(self):
        if not self._checked:
            try:
                self._client.admin.command("ping")
            except PyMongoError as e:
                raise StoreError(f"Cannot reach MongoDB at {self._safe_uri()}: {e}. "
                                 "Is the MongoDB service running and MONGODB_URI correct?") from e
            self._checked = True
        return self._client[self.database]

    def _safe_uri(self) -> str:
        # never echo credentials from mongodb://user:pass@host
        head, sep, tail = self.uri.rpartition("@")
        return f"{self.uri.split('://', 1)[0]}://***@{tail}" if sep else self.uri

    def _call(self, fn):
        try:
            return fn()
        except StoreError:
            raise
        except PyMongoError as e:
            raise StoreError(f"MongoDB error: {e}") from e

    # -- index
    def load_index(self) -> dict | None:
        def run():
            entries = [_strip(d) for d in self.db.candidates.find()]
            if not entries:
                return None
            return {"schema_version": _SCHEMA_VERSION,
                    "updated_at": max(e.get("updated_at") or "" for e in entries),
                    "candidates": {e["candidate_id"]: e for e in entries}}
        return self._call(run)

    def save_index(self, doc: dict, changed: list[str] | None = None) -> None:
        cands = doc.get("candidates", {})
        ids = list(cands) if changed is None else [c for c in changed if c in cands]
        if not ids:
            return
        ops = [ReplaceOne({"_id": cid}, {"_id": cid, **cands[cid]}, upsert=True) for cid in ids]
        self._call(lambda: self.db.candidates.bulk_write(ops, ordered=False))

    # -- records
    def put_record(self, collection: Collection, key: str, doc: dict) -> str:
        self._call(lambda: self.db[collection].replace_one({"_id": key}, {"_id": key, **doc}, upsert=True))
        return mongo_ref(collection, key)

    def ref(self, collection: Collection, key: str) -> str:
        return mongo_ref(collection, key)

    def get_record(self, ref: str) -> dict | None:
        parsed = parse_mongo_ref(ref)
        if parsed is None:  # a file ref from before the switch to MongoDB
            try:
                return json.loads(resolve_stored(ref).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
        collection, key = parsed
        return _strip(self._call(lambda: self.db[collection].find_one({"_id": key})))

    def put_text(self, collection: TextCollection, key: str, text: str) -> str:
        self._call(lambda: self.db[collection].replace_one({"_id": key}, {"_id": key, "text": text}, upsert=True))
        return mongo_ref(collection, key)

    def get_text(self, ref: str) -> str | None:
        parsed = parse_mongo_ref(ref)
        if parsed is None:
            try:
                return resolve_stored(ref).read_text(encoding="utf-8")
            except OSError:
                return None
        collection, key = parsed
        doc = self._call(lambda: self.db[collection].find_one({"_id": key}))
        return doc.get("text") if doc else None

    # -- invites
    def load_invites(self) -> dict[str, dict]:
        return self._call(lambda: {d["token"]: _strip(d) for d in self.db.invites.find()})

    def save_invites(self, invites: dict[str, dict], changed: list[str]) -> None:
        ops = [ReplaceOne({"_id": t}, {"_id": t, **invites[t]}, upsert=True) for t in changed if t in invites]
        if ops:
            self._call(lambda: self.db.invites.bulk_write(ops, ordered=False))

    # -- failures
    def add_failure(self, line: dict) -> None:
        self._call(lambda: self.db.failures.insert_one(dict(line)))

    def failures(self) -> list[dict]:
        return self._call(lambda: [_strip(d) for d in self.db.failures.find().sort("_id", 1)])

    def describe(self) -> str:
        return f"mongodb ({self._safe_uri()}, database {self.database})"
