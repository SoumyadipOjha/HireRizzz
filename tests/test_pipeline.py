"""End-to-end Stage 0-3 with FakeLLM, incl. log-and-skip behaviour."""

from __future__ import annotations

import json
import uuid

from conftest import SAMPLES, FakeLLM

from screening.schemas import Stage1Record, Stage2Record, Stage3Record
from screening.stage0_ingest import ingest
from screening.stage1_extract import run_stage1
from screening.stage2_shortlist import run_stage2
from screening.stage3_call import parse_local_transcript, run_stage3


def _by_file(ctx):
    return {e.source_file.rsplit("/", 1)[-1]: e for e in ctx.index.all()}


def _failures(ctx):
    p = ctx.config.data.failures_log
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def test_full_pipeline(ctx, data_dir):
    resumes = data_dir / "input" / "resumes"
    (resumes / "broken.docx").write_text("not a zip", encoding="utf-8")
    (resumes / "cv.pdf").write_bytes(b"%PDF-1.4")
    (resumes / "~$aarav_sharma.docx").write_bytes(b"lock")

    # Stage 0
    res = ingest(ctx)
    assert len(res.new) == 5  # lock file ignored, bad files still registered
    for cid in res.new:
        assert uuid.UUID(cid).version == 4
    assert ingest(ctx).new == []  # idempotent

    llm = FakeLLM()

    # Stage 1
    s1 = run_stage1(ctx, llm)
    assert len(s1.succeeded) == 3 and len(s1.failed) == 2
    files = _by_file(ctx)
    assert files["broken.docx"].stages["stage1_extraction"].status == "failed"
    assert "DocxReadError" in files["broken.docx"].stages["stage1_extraction"].error
    assert "Unsupported file type" in files["cv.pdf"].stages["stage1_extraction"].error
    fails = _failures(ctx)
    assert {f["candidate_id"] for f in fails} == {files["broken.docx"].candidate_id, files["cv.pdf"].candidate_id}
    assert all(f["stage"] == "stage1_extraction" and f["run_id"] == ctx.run_id for f in fails)

    aarav = files["aarav_sharma.docx"]
    rec1 = Stage1Record.model_validate_json((ctx.config.data.stage1_output / f"{aarav.candidate_id}.json").read_text())
    assert rec1.candidate_id == aarav.candidate_id and rec1.extraction.full_name == "Aarav Sharma"
    assert aarav.display_name == "Aarav Sharma"
    assert run_stage1(ctx, llm).already_done  # no re-work on rerun

    # Stage 2
    s2 = run_stage2(ctx, llm)
    assert len(s2.succeeded) == 3 and len(s2.skipped) == 2
    files = _by_file(ctx)
    st = files["aarav_sharma.docx"].stages["stage2_shortlisting"]
    assert (st.decision, st.score) == ("shortlisted", 89.75)
    assert files["priya_nair.docx"].stages["stage2_shortlisting"].decision == "rejected"
    assert files["priya_nair.docx"].overall_status == "rejected"
    assert files["broken.docx"].stages["stage2_shortlisting"].status == "skipped"

    rec2 = Stage2Record.model_validate_json(
        (ctx.config.data.stage2_output / f"{aarav.candidate_id}.json").read_text())
    assert rec2.matched_nice_to_have_skills == ["FastAPI", "Docker", "AWS"]  # 'Kubernetes' not in JD -> dropped
    assert rec2.matched_must_have_skills == ["Python", "REST API development", "SQL", "Git"]  # JD wording
    rohan = Stage2Record.model_validate_json(
        (ctx.config.data.stage2_output / f"{files['rohan_mehta.docx'].candidate_id}.json").read_text())
    assert rohan.missing_must_have_skills == ["Python", "REST API development", "SQL"]  # omitted -> missing

    # Stage 3: interview links for shortlisted candidates only
    s3 = run_stage3(ctx)
    assert s3.awaiting == [aarav.candidate_id]
    assert len(s3.skipped) == 4 and not s3.failed
    files = _by_file(ctx)
    st3 = files["aarav_sharma.docx"].stages["stage3_calling"]
    assert st3.status == "awaiting" and "/interview/" in st3.note
    assert files["aarav_sharma.docx"].overall_status == "awaiting"
    assert "not shortlisted" in files["priya_nair.docx"].stages["stage3_calling"].note
    link1 = st3.note.rsplit("/", 1)[-1]
    assert run_stage3(ctx).awaiting == [aarav.candidate_id]  # re-run keeps the same link
    assert ctx.index.get(aarav.candidate_id).stages["stage3_calling"].note.endswith(link1)

    # Transcript parser for a call held elsewhere
    out = parse_local_transcript(ctx, llm, aarav.candidate_id, SAMPLES / "transcripts" / "aarav_sharma_call.txt")
    rec3 = Stage3Record.model_validate(ctx.config.store.get_record(out))
    assert rec3.candidate_id == aarav.candidate_id and rec3.call.channel == "local_transcript_file"
    ids = [a.question_id for a in rec3.screening.answers]
    assert ids == [q.id for q in ctx.config.questions.questions]  # one per configured question, unknown dropped
    assert not next(a for a in rec3.screening.answers if a.question_id == "expected_ctc").answered
    assert _by_file(ctx)["aarav_sharma.docx"].overall_status == "completed"

    # Index on disk is valid and complete
    doc = json.loads(ctx.config.data.candidates_index.read_text(encoding="utf-8"))
    assert len(doc["candidates"]) == 5


def test_llm_failure_is_logged_and_batch_continues(ctx):
    ingest(ctx)
    s1 = run_stage1(ctx, FakeLLM(fail_on={"Priya Nair"}))
    assert len(s1.succeeded) == 2 and len(s1.failed) == 1
    priya = _by_file(ctx)["priya_nair.docx"]
    assert priya.overall_status == "failed"
    assert "LLMResponseError" in priya.stages["stage1_extraction"].error
    assert _failures(ctx)[0]["candidate_id"] == priya.candidate_id

    # A later run retries only the failed one
    llm = FakeLLM()
    s1b = run_stage1(ctx, llm)
    assert s1b.succeeded == [priya.candidate_id] and len(s1b.already_done) == 2
    assert _by_file(ctx)["priya_nair.docx"].overall_status == "active"


def test_rerun_upstream_resets_downstream(ctx):
    ingest(ctx)
    llm = FakeLLM()
    run_stage1(ctx, llm)
    run_stage2(ctx, llm)
    aarav = _by_file(ctx)["aarav_sharma.docx"]
    run_stage1(ctx, llm, force=True, only={aarav.candidate_id})
    assert ctx.index.get(aarav.candidate_id).stages["stage2_shortlisting"].status == "pending"


def test_join_key_mismatch_fails_candidate(ctx):
    ingest(ctx)
    llm = FakeLLM()
    run_stage1(ctx, llm)
    aarav = _by_file(ctx)["aarav_sharma.docx"]
    p = ctx.config.data.stage1_output / f"{aarav.candidate_id}.json"
    data = json.loads(p.read_text())
    data["candidate_id"] = str(uuid.uuid4())
    p.write_text(json.dumps(data))
    s2 = run_stage2(ctx, llm)
    assert aarav.candidate_id in s2.failed
    assert "JoinKeyMismatchError" in ctx.index.get(aarav.candidate_id).stages["stage2_shortlisting"].error


def test_changed_resume_keeps_candidate_id(ctx, data_dir):
    ingest(ctx)
    run_stage1(ctx, FakeLLM())
    before = _by_file(ctx)["priya_nair.docx"].candidate_id
    f = data_dir / "input" / "resumes" / "priya_nair.docx"
    f.write_bytes(f.read_bytes() + b"\0")  # content changed, same file name
    res = ingest(ctx)
    assert res.updated == [before]
    assert ctx.index.get(before).stages["stage1_extraction"].status == "pending"


def test_source_modified_after_ingest_fails(ctx, data_dir):
    ingest(ctx)
    f = data_dir / "input" / "resumes" / "rohan_mehta.docx"
    f.write_bytes(f.read_bytes() + b"\0")
    s1 = run_stage1(ctx, FakeLLM())
    assert len(s1.failed) == 1
    assert "SourceChangedError" in _by_file(ctx)["rohan_mehta.docx"].stages["stage1_extraction"].error
