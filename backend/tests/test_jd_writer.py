"""Gate 1: brief -> AI draft (not live) -> manager approves -> posted as a new job (the others untouched)."""

from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer

import pytest
from conftest import FakeLLM

from screening.config import ScreeningQuestion
from screening.dashboard.server import DashboardAPI, Handler
from screening.jd_writer import JDError, approve_jd, draft_jd, load_draft, merge_questions
from screening.paths import CONFIG_DIR

BRIEF = "We need a data engineer in Pune, 2-5 years, Python, SQL and Airflow. Young, energetic team!"


@pytest.fixture
def jd_ctx(ctx, tmp_path):
    """Point the config at copies of the JD/questions files so the real ones are never touched."""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    for name in ("job_description.yaml", "screening_questions.yaml"):
        shutil.copy(CONFIG_DIR / name, cfg_dir / name)
    ctx.config.settings.files.job_description = str(cfg_dir / "job_description.yaml")
    ctx.config.settings.files.screening_questions = str(cfg_dir / "screening_questions.yaml")
    ctx.config.reload_files()
    return ctx, cfg_dir


def test_draft_is_not_live_until_approved(jd_ctx):
    ctx, cfg_dir = jd_ctx
    before = (cfg_dir / "job_description.yaml").read_text(encoding="utf-8")
    d = draft_jd(ctx.config, FakeLLM(), BRIEF)
    assert d["job"]["title"] == "Data Engineer" and d["job"]["company_name"] == "Kanerika Inc"
    assert d["job"]["job_id"].startswith("data-engineer-")
    assert d["language_notes"] and load_draft(ctx.config)["job"]["title"] == "Data Engineer"
    assert (cfg_dir / "job_description.yaml").read_text(encoding="utf-8") == before  # nothing published yet
    assert ctx.config.job.title == "Python Backend Engineer"

    kinds = [(q["id"], q["kind"]) for q in d["questions"]]
    assert kinds[:3] == [("interest", "logistics"), ("role_1", "role"), ("role_2", "role")]
    assert "recent_project" not in dict(kinds) and ("notice_period", "logistics") in kinds  # logistics kept

    r = approve_jd(ctx.config, d["job"], d["questions"], by="Dev (Manager)")
    assert r["job_id"].startswith("data-engineer-") and not r["updated"]
    new = ctx.config.for_job(r["job_id"])                                  # posted as a second job
    assert new.job.title == "Data Engineer" and new.job.approved_by == "Dev (Manager)"
    assert [q.id for q in new.questions.questions if q.kind == "role"] == ["role_1", "role_2"]
    assert new.job.must_have_skills == ["Python", "SQL", "Airflow"]
    assert ctx.config.job.title == "Python Backend Engineer"                # the first job is untouched
    assert (cfg_dir / "job_description.yaml").read_text(encoding="utf-8") == before
    assert {j.job["job_id"] for j in ctx.config.jobs.all()} == {ctx.config.job.job_id, r["job_id"]}
    assert load_draft(ctx.config) is None

    again = approve_jd(ctx.config, d["job"], d["questions"], by="Dev")     # same draft twice: a new id
    assert again["job_id"] == r["job_id"] + "-2"
    upd = approve_jd(ctx.config, {**d["job"], "title": "Lead Data Engineer"}, d["questions"], by="Dev",
                     job_id=r["job_id"], scored_candidates=3)             # editing a posted job
    assert upd["updated"] and upd["already_scored"] == 3
    assert ctx.config.for_job(r["job_id"]).job.title == "Lead Data Engineer"


def test_bad_briefs_and_drafts_are_refused(jd_ctx):
    ctx, _ = jd_ctx
    with pytest.raises(JDError, match="at least a sentence"):
        draft_jd(ctx.config, FakeLLM(), "dev")
    d = draft_jd(ctx.config, FakeLLM(), BRIEF)
    with pytest.raises(JDError, match="who is approving"):
        approve_jd(ctx.config, d["job"], d["questions"], by="")
    with pytest.raises(JDError, match="must_have_skills"):
        approve_jd(ctx.config, {**d["job"], "must_have_skills": []}, d["questions"], by="M")
    with pytest.raises(JDError, match="above the maximum"):
        approve_jd(ctx.config, {**d["job"], "min_experience_years": 9, "max_experience_years": 2}, d["questions"], by="M")
    assert ctx.config.job.title == "Python Backend Engineer"


def test_merge_questions_keeps_logistics_in_place():
    cur = [ScreeningQuestion(id="a", question="A?"), ScreeningQuestion(id="r", question="R?", kind="role"),
           ScreeningQuestion(id="b", question="B?")]
    assert [q.id for q in merge_questions(cur, ["X?", "Y?"])] == ["a", "role_1", "role_2", "b"]
    assert [q.id for q in merge_questions([ScreeningQuestion(id="a", question="A?")], ["X?"])] == ["a", "role_1"]


def test_jd_writer_over_http(jd_ctx):
    ctx, _ = jd_ctx
    api = DashboardAPI(ctx.config, ctx=ctx, llm_factory=lambda: FakeLLM())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, api=api))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        with urllib.request.urlopen(base + "/api/jd/draft") as r:
            assert json.loads(r.read()) == {"draft": None}
        status, body = post("/api/jd/draft", {"brief": BRIEF})
        assert status == 200 and body["draft"]["job"]["title"] == "Data Engineer"
        assert post("/api/jd/draft", {"brief": "x"})[0] == 400
        d = body["draft"]
        status, body = post("/api/jd/approve", {"by": "Dev", "job": {**d["job"], "title": "Senior Data Engineer"},
                                                "questions": d["questions"]})
        assert status == 200 and body["title"] == "Senior Data Engineer"
        jid = body["job_id"]
        with urllib.request.urlopen(base + "/api/overview?job=" + jid) as r:
            assert json.loads(r.read())["job"]["title"] == "Senior Data Engineer"
        with urllib.request.urlopen(base + "/api/jobs") as r:
            jobs = json.loads(r.read())["jobs"]
        assert [j["title"] for j in jobs] == ["Senior Data Engineer", "Python Backend Engineer"]  # newest first
        with urllib.request.urlopen(base + "/api/jobs/" + jid) as r:
            job = json.loads(r.read())
        assert job["status"] == "open" and job["candidates"] == [] and len(job["questions"]) == len(d["questions"])
        assert post(f"/api/jobs/{jid}/status", {"status": "closed"}) == (200, {"job_id": jid, "status": "closed"})
        assert post("/api/resumes", {"job_id": jid, "files": [{"name": "a.pdf", "data": ""}]})[0] == 400  # closed
        try:
            urllib.request.urlopen(base + "/api/jobs/no-such-job")
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
