"""Dashboard server: serves the page and read-only JSON, rejects bad ids."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer

import pytest
from conftest import FakeLLM

from screening.dashboard.server import DashboardAPI, Handler
from screening.stage0_ingest import ingest
from screening.stage1_extract import run_stage1
from screening.stage2_shortlist import run_stage2


@pytest.fixture
def server(ctx):
    ingest(ctx)
    run_stage1(ctx, FakeLLM())
    run_stage2(ctx, FakeLLM())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, api=DashboardAPI(ctx.config)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", ctx
    httpd.shutdown()
    httpd.server_close()


def _get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def test_page_and_api(server):
    base, ctx = server
    status, ctype, body = _get(base + "/")
    assert status == 200 and ctype.startswith("text/html") and b"Screening Dashboard" in body

    ov = json.loads(_get(base + "/api/overview")[2])
    assert len(ov["index"]["candidates"]) == 3 and ov["demo"] is False
    assert ov["job"]["job_id"] and ov["thresholds"]["shortlist_score"] == 70

    cid = next(iter(ov["index"]["candidates"]))
    detail = json.loads(_get(f"{base}/api/candidate/{cid}")[2])
    assert detail["records"]["stage1_extraction"]["candidate_id"] == cid
    assert detail["records"]["stage2_shortlisting"]["candidate_id"] == cid
    assert detail["records"]["stage3_calling"] is None


@pytest.mark.parametrize("path,code", [
    ("/api/candidate/..%2F..%2Fconfig", 400),
    ("/api/candidate/not-a-uuid", 400),
    ("/api/candidate/00000000-0000-4000-8000-000000000000", 404),
    ("/etc/passwd", 404),
])
def test_bad_requests(server, path, code):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base + path)
    assert e.value.code == code


def test_upload_resumes_screens_them(ctx):
    import base64
    import time

    from conftest import SAMPLES

    api = DashboardAPI(ctx.config, ctx=ctx, llm_factory=lambda: FakeLLM())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, api=api))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(body):
        req = urllib.request.Request(base + "/api/resumes", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        for f in ctx.config.data.input_resumes.iterdir():
            f.unlink()
        data = base64.b64encode((SAMPLES / "resumes" / "aarav_sharma.docx").read_bytes()).decode()
        status, r = post({"files": [{"name": "../../Aarav Sharma CV.docx", "data": data}]})
        assert status == 200 and r["saved"] == ["Aarav_Sharma_CV.docx"]  # path parts stripped
        assert (ctx.config.data.input_resumes / "Aarav_Sharma_CV.docx").exists()
        for _ in range(100):
            if not api.processing["running"]:
                break
            time.sleep(0.1)
        [entry] = ctx.index.reload().all()
        assert entry.display_name == "Aarav Sharma" and entry.stages["stage2_shortlisting"].decision == "shortlisted"
        assert api.processing["message"].startswith("Done: 1 scored")
        assert post({"files": [{"name": "cv.exe", "data": data}]})[0] == 400
        assert post({"files": [{"name": "cv.pdf", "data": "not base64!!"}]})[0] == 400
    finally:
        httpd.shutdown()
        httpd.server_close()
