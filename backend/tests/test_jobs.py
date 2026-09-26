"""Several posted jobs: each has its own resumes folder, JD, questions and board."""

from __future__ import annotations

import shutil

import pytest
from conftest import FakeLLM

from screening.approvals import approve_shortlist, board_column
from screening.jobs import JobError
from screening.stage0_ingest import ingest, resumes_dir
from screening.stage1_extract import run_stage1
from screening.stage2_shortlist import run_stage2


def _post_second_job(ctx):
    first = ctx.config.jobs.get(None)
    job = {**first.job, "title": "Data Engineer", "must_have_skills": ["Python", "SQL", "Airflow"]}
    job.pop("job_id")
    return ctx.config.jobs.create(job, first.questions, posted_by="Dev").job["job_id"]


def test_the_yaml_job_becomes_the_default_job(ctx):
    jobs = ctx.config.jobs.all()
    assert len(jobs) == 1 and jobs[0].is_default and jobs[0].status == "open"
    assert ctx.config.job.job_id == jobs[0].job["job_id"] == ctx.config.jobs.default_id()
    assert ctx.config.store.list_records("jobs")  # persisted: later runs read the store, not the YAML
    with pytest.raises(JobError, match="unknown job"):
        ctx.config.jobs.get("nope")


def test_candidates_belong_to_the_job_they_applied_to(ctx):
    default = ctx.config.job.job_id
    second = _post_second_job(ctx)
    folder = resumes_dir(ctx, second)
    folder.mkdir(parents=True)
    one = next(ctx.config.data.input_resumes.glob("*.docx"))
    shutil.copy(one, folder / one.name)            # the same resume applies to both jobs

    ingest(ctx)                                     # default job: input/resumes/
    ingest(ctx, job_id=second)                      # input/resumes/<job_id>/
    ingest(ctx, job_id=second)                      # re-running is a no-op
    entries = ctx.index.all()
    by_job = {}
    for e in entries:
        by_job.setdefault(ctx.config.jobs.candidate_job(e), []).append(e)
    assert len(by_job[second]) == 1 and len(by_job[default]) == len(entries) - 1
    assert all(e.job_id is None for e in by_job[default])  # stored like candidates from before jobs

    llm = FakeLLM()
    run_stage1(ctx, llm)
    run_stage2(ctx, llm)
    prompts = [p for kind, p in llm.calls if kind == "ShortlistAssessmentLLM"]
    assert sum(f"Job ID: {second}\n" in p and "Airflow" in p for p in prompts) == 1  # scored against its own JD
    assert sum(f"Job ID: {default}\n" in p for p in prompts) == len(entries) - 1
    assert {ctx.config.store.get_record(e.stages["stage2_shortlisting"].output_path)["job_id"]
            for e in ctx.index.reload().all()} == {default, second}


def test_board_columns_follow_the_pipeline(ctx):
    ctx.config.settings.approvals.require_shortlist_approval = True
    ingest(ctx)
    run_stage1(ctx, FakeLLM())
    assert {board_column(e) for e in ctx.index.all()} == {"applied"}
    run_stage2(ctx, FakeLLM())
    assert {board_column(e) for e in ctx.index.all()} == {"resume_review"}  # AI suggests, recruiter decides
    decisions = {e.candidate_id: e.stages["stage2_shortlisting"].decision for e in ctx.index.all()}
    approve_shortlist(ctx, decisions, by="Asha")
    cols = {board_column(e) for e in ctx.index.all()}
    assert cols <= {"interview", "rejected"} and "rejected" in cols


def test_closed_jobs_and_unknown_jobs(ctx):
    second = _post_second_job(ctx)
    assert ctx.config.jobs.set_status(second, "closed").status == "closed"
    assert ctx.config.jobs.refresh().get(second).status == "closed"   # persisted
    with pytest.raises(JobError):
        ingest(ctx, job_id="no-such-job")
