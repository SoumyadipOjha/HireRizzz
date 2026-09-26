"""The pipeline on the MongoDB backend, against a real server (skipped if none is reachable).

Each test gets its own throwaway database, dropped afterwards.
Point MONGODB_TEST_URI at a server to run these elsewhere (default: local).
"""

from __future__ import annotations

import os
import uuid

import pytest
from conftest import FakeLLM, turn

pymongo = pytest.importorskip("pymongo")

from screening.agent.dialogue import ScreeningDialogue  # noqa: E402
from screening.agent.invites import InviteError, InviteStore  # noqa: E402
from screening.config import load_config  # noqa: E402
from screening.context import RunContext  # noqa: E402
from screening.dashboard.server import DashboardAPI  # noqa: E402
from screening.index import CandidateIndex  # noqa: E402
from screening.schemas import Stage1Record, Stage3Record  # noqa: E402
from screening.stage0_ingest import ingest  # noqa: E402
from screening.stage1_extract import run_stage1  # noqa: E402
from screening.stage2_shortlist import load_stage1, run_stage2  # noqa: E402
from screening.stage3_call import finalize_dialogue, run_stage3  # noqa: E402
from screening.storage import StoreError  # noqa: E402
from screening.storage.mongo_store import MongoStore  # noqa: E402

URI = os.environ.get("MONGODB_TEST_URI", "mongodb://localhost:27017")


def _reachable() -> bool:
    try:
        pymongo.MongoClient(URI, serverSelectionTimeoutMS=1500).admin.command("ping")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason=f"no MongoDB at {URI}")


@pytest.fixture
def mctx(data_dir, monkeypatch):
    db = f"screening_test_{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("MONGODB_URI", URI)
    monkeypatch.setenv("MONGODB_DB_NAME", db)
    ctx = RunContext.create(load_config(data_dir=data_dir, storage="mongodb"))
    yield ctx
    pymongo.MongoClient(URI).drop_database(db)


def test_pipeline_on_mongodb(mctx):
    ctx = mctx
    store = ctx.config.store
    assert store.backend == "mongodb"
    ingest(ctx)
    run_stage1(ctx, FakeLLM())
    run_stage2(ctx, FakeLLM())
    s3 = run_stage3(ctx)

    db = store.db
    assert db.candidates.count_documents({}) == 3
    aarav = next(e for e in ctx.index.all() if e.display_name == "Aarav Sharma")
    s1_ref = aarav.stages["stage1_extraction"].output_path
    assert s1_ref == f"mongodb://stage1_extracted/{aarav.candidate_id}"
    assert load_stage1(aarav, store).extraction.full_name == "Aarav Sharma"
    assert db.stage2_shortlist.count_documents({}) == 3
    assert s3.awaiting == [aarav.candidate_id]
    assert db.invites.count_documents({"candidate_id": aarav.candidate_id, "status": "active"}) == 1
    # nothing was written to the JSON-file layout
    assert not ctx.config.data.candidates_index.exists()
    assert not any(ctx.config.data.stage1_output.iterdir())

    # a fresh index object sees the same data (another process, e.g. the server)
    again = CandidateIndex(load_config(data_dir=ctx.config.data.root, storage="mongodb").store)
    assert {e.candidate_id for e in again.all()} == {e.candidate_id for e in ctx.index.all()}

    # screening call end to end -> transcript + stage3 record in MongoDB, link used
    n = len(ctx.config.questions.questions)
    llm = FakeLLM(agent_script=[turn("consent_yes", True, "Great.")] +
                  [turn("answer", True, "Thanks.") for _ in range(n)])
    d = ScreeningDialogue(config=ctx.config, llm=llm, candidate_id=aarav.candidate_id, candidate_name="Aarav Sharma",
                          channel="browser_text")
    d.start()
    for a in ["yes"] + [f"answer {i}" for i in range(n)]:
        if d.ended:
            break
        d.reply(a)
    ref = finalize_dialogue(ctx, llm, d)
    assert ref == f"mongodb://stage3_calls/{aarav.candidate_id}"
    rec = Stage3Record.model_validate(store.get_record(ref))
    assert "Candidate: answer 0" in store.get_text(rec.transcript_path)
    entry = ctx.index.reload().get(aarav.candidate_id)
    assert entry.overall_status == "completed"
    token = db.invites.find_one({"candidate_id": aarav.candidate_id})["token"]
    with pytest.raises(InviteError, match="already been completed"):
        InviteStore(store).validate(token)

    # the dashboard reads everything back from MongoDB
    api = DashboardAPI(ctx.config)
    assert "mongodb" in api.overview()["storage"]
    detail = api.candidate(aarav.candidate_id)
    assert Stage1Record.model_validate(detail["records"]["stage1_extraction"]).candidate_id == aarav.candidate_id
    assert "Candidate: answer 0" in detail["transcript"]


def test_failures_go_to_mongodb(mctx, data_dir):
    (data_dir / "input" / "resumes" / "broken.docx").write_text("not a zip", encoding="utf-8")
    ingest(mctx)
    run_stage1(mctx, FakeLLM())
    fails = mctx.config.store.failures()
    assert [f["error_type"] for f in fails] == ["DocxReadError"]
    assert not mctx.config.data.failures_log.exists()


def test_unreachable_mongodb_is_a_clear_error(data_dir):
    store = MongoStore("mongodb://user:secret@127.0.0.1:1", "x", timeout_ms=300)
    with pytest.raises(StoreError, match="Cannot reach MongoDB") as e:
        store.load_index()
    assert "secret" not in str(e.value)
