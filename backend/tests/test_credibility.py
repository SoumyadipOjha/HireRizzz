"""Resume credibility: code checks on the resume, and the LinkedIn PDF cross-check."""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request
from datetime import date
from functools import partial
from http.server import ThreadingHTTPServer

from conftest import PROFILES, FakeLLM
from test_pdf import make_pdf

from screening.credibility import linkedin_checks, load_record, resume_checks, same_company
from screening.dashboard.server import DashboardAPI, Handler
from screening.paths import resolve_stored
from screening.schemas import ResumeExtractionLLM
from screening.stage0_ingest import ingest
from screening.stage1_extract import run_stage1

TODAY = date(2026, 9, 27)


def job(title, company, start, end=None, current=False):
    return {"title": title, "company": company, "location": None, "start_date": start, "end_date": end,
            "is_current": current, "highlights": []}


def profile(jobs, years=None, grad=2019, name="Ravi Kumar"):
    return ResumeExtractionLLM.model_validate({
        "full_name": name, "email": "r@x.com", "phone": None, "location": None, "linkedin_url": None,
        "headline": None, "summary": None, "total_experience_years": years, "current_title": None,
        "current_company": None, "skills": [], "work_experience": jobs,
        "education": [{"degree": "B.Tech", "field_of_study": "CS", "institution": "X", "graduation_year": grad}],
        "certifications": [], "languages": []})


def checks(flags):
    return {(f.check, f.level) for f in flags}


def test_clean_resume_has_no_flags():
    p = profile([job("Engineer", "Acme", "2022-01", current=True), job("Dev", "Beta", "2019-07", "2021-12")], years=7)
    assert resume_checks(p, "Python developer.", TODAY) == []


def test_resume_red_and_amber_flags():
    p = profile([job("Senior Engineer", "Acme", "2023-01", current=True),
                 job("Engineer", "Beta", "2022-01", "2024-06"),          # overlaps Acme by 17 months
                 job("Developer", "Gamma", "2016-01", "2017-01"),        # full-time 3 years before graduating
                 job("Lead", "Delta", None)],                            # no dates
                years=12)                                                # dates add up to ~6
    text = "I have 9 years of FastAPI experience and 3 years with Docker."
    got = checks(resume_checks(p, text, TODAY))
    assert ("timeline_overlap", "amber") in got
    assert ("experience_inflated", "red") in got
    assert ("work_before_graduation", "amber") in got
    assert ("missing_dates", "amber") in got
    assert ("skill_older_than_tech", "red") in got                       # FastAPI is from 2018
    assert not any("docker" in f.message.lower() for f in resume_checks(p, text, TODAY)
                   if f.check == "skill_older_than_tech")                # 3 years of Docker is fine


def test_every_years_of_claim_is_checked():
    from screening.credibility import _years_of_claims

    text = "Brings 5 years of LangChain and 6 years of Microsoft Fabric expertise, 3+ yrs with Docker."
    assert _years_of_claims(text) == [(5.0, "langchain"), (6.0, "microsoft fabric"), (3.0, "docker")]


def test_internships_are_not_flagged_as_overlap_or_early_work():
    p = profile([job("Engineer", "Acme", "2019-07", current=True), job("Intern", "Beta", "2017-01", "2019-12")])
    assert resume_checks(p, "", TODAY) == []


def test_impossible_dates():
    p = profile([job("Engineer", "Acme", "2027-05", current=True), job("Dev", "Beta", "2021-05", "2020-01")])
    got = checks(resume_checks(p, "", TODAY))
    assert ("future_date", "red") in got and ("end_before_start", "red") in got


def test_company_names_match_despite_suffixes():
    assert same_company("Kanerika Software Pvt. Ltd.", "Kanerika")
    assert same_company("FinStack Technologies", "Finstack Technologies Private Limited")
    assert not same_company("Acme", "Beta Labs")


def test_linkedin_cross_check():
    resume = profile([job("Senior Engineer", "FinStack Technologies", "2019-01", current=True),
                      job("Engineer", "CloudKart Pvt Ltd", "2016-07", "2018-12"),
                      job("Developer", "Ghost Corp", "2015-01", "2016-06")], years=10)
    linkedin = profile([job("Software Engineer", "FinStack", "2022-03", current=True),   # started 3 years later
                        job("Engineer", "CloudKart", "2016-08", "2018-12"),              # within tolerance
                        job("Analyst", "Real Co", "2020-01", "2022-02")], years=5)        # not on the resume
    flags = linkedin_checks(resume, linkedin, TODAY)
    by = {f.check: f for f in flags if f.level != "green"}
    assert by["linkedin_dates"].level == "red" and "38 months apart" in by["linkedin_dates"].message
    assert "Jan 2019" in by["linkedin_dates"].resume and "Mar 2022" in by["linkedin_dates"].linkedin
    assert by["linkedin_missing_company"].level == "red" and "Ghost Corp" in by["linkedin_missing_company"].message
    assert by["linkedin_extra_company"].level == "amber" and "Real Co" in by["linkedin_extra_company"].message
    assert by["linkedin_total_experience"].level == "amber"
    assert [f.message for f in flags if f.level == "green"] == ["CloudKart Pvt Ltd: title and dates match LinkedIn."]


def test_linkedin_for_someone_else_is_red():
    a = profile([job("Engineer", "Acme", "2020-01", current=True)], name="Ravi Kumar")
    b = profile([job("Engineer", "Acme", "2020-01", current=True)], name="Sneha Iyer")
    assert ("linkedin_name", "red") in checks(linkedin_checks(a, b, TODAY))


def test_checks_run_after_stage1_and_linkedin_upload_over_http(ctx, tmp_path):
    ingest(ctx)
    run_stage1(ctx, FakeLLM())
    aarav = next(e for e in ctx.index.all() if e.display_name == "Aarav Sharma")
    rec = load_record(ctx.config, aarav.candidate_id)
    assert rec is not None and ctx.index.get(aarav.candidate_id).credibility is not None  # ran with Stage 1

    api = DashboardAPI(ctx.config, ctx=ctx, llm_factory=lambda: FakeLLM())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, api=api))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/api/candidate/{aarav.candidate_id}/linkedin"

    def post(body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        pdf = tmp_path / "li.pdf"
        # a LinkedIn export with the same career (FakeLLM "reads" Aarav's profile from it)
        make_pdf(pdf, ["Aarav Sharma", "Senior Software Engineer at Acme", "Experience"] + ["Acme 2022 - Present"] * 20)
        status, r = post({"name": "Profile.pdf", "data": base64.b64encode(pdf.read_bytes()).decode()})
        assert status == 200 and r["summary"]["linkedin"] is True and r["summary"]["red"] == 0
        assert r["summary"]["green"] == len(PROFILES["Aarav Sharma"]["work_experience"])
        assert ctx.index.reload().get(aarav.candidate_id).credibility.linkedin
        assert (ctx.config.data.root / "input" / "linkedin" / f"{aarav.candidate_id}.pdf").exists()

        assert post({"name": "x.docx", "data": "abc"})[0] == 400
        assert post({"name": "x.pdf", "data": base64.b64encode(b"not a pdf").decode()})[0] == 400
        empty = tmp_path / "empty.pdf"
        make_pdf(empty, [])
        status, r = post({"name": "e.pdf", "data": base64.b64encode(empty.read_bytes()).decode()})
        assert status == 400 and "Save to PDF" in r["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_resume_file_is_served_from_disk_or_the_stored_copy(ctx):
    ingest(ctx)
    run_stage1(ctx, FakeLLM())
    aarav = next(e for e in ctx.index.all() if e.display_name == "Aarav Sharma")
    api = DashboardAPI(ctx.config, ctx=ctx, llm_factory=lambda: FakeLLM())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, api=api))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/api/candidate/{aarav.candidate_id}/resume"
    try:
        with urllib.request.urlopen(url) as r:
            f = json.loads(r.read())
        original = resolve_stored(aarav.source_file)
        assert f["name"] == original.name and base64.b64decode(f["data"]) == original.read_bytes()
        assert "Aarav Sharma" in f["text"]

        original.unlink()  # e.g. a host whose disk was wiped: the copy from Stage 1 is served
        with urllib.request.urlopen(url) as r:
            f = json.loads(r.read())
        assert f["data"] and "Aarav Sharma" in f["text"]
    finally:
        httpd.shutdown()
        httpd.server_close()
