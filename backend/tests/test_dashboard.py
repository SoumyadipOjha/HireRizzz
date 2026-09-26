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


@pytest.fixture
def built_frontend(tmp_path, monkeypatch):
    """A stand-in for `npm run build` output (frontend/dist)."""
    dist = tmp_path / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<div id="root"></div><script src="/assets/app.js"></script>', encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "interview.html").write_text("<title>interview</title>", encoding="utf-8")
    monkeypatch.setenv("HIRERIZZ_FRONTEND_DIR", str(tmp_path / "frontend"))
    return dist


def test_page_and_api(server, built_frontend):
    base, ctx = server
    status, ctype, body = _get(base + "/")
    assert status == 200 and ctype.startswith("text/html") and b'<div id="root">' in body
    for path in ("/jobs", "/jobs/some-job?c=x"):   # the app's own routes load the app
        assert b'<div id="root">' in _get(base + path)[2]
    status, ctype, body = _get(base + "/assets/app.js")
    assert status == 200 and ctype.startswith("application/javascript") and body == b"console.log(1)"
    for bad in ("/assets/missing.js", "/assets/..%2F..%2Fsecret.txt"):
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(base + bad)
        assert e.value.code == 404

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


def test_cors_only_for_the_configured_frontend(server, monkeypatch):
    base, _ = server
    monkeypatch.setenv("CORS_ORIGINS", "https://hirerizz.vercel.app, https://hirerizz-*.vercel.app")

    def call(method, origin):
        req = urllib.request.Request(base + "/api/overview", method=method, headers={"Origin": origin})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers.get("Access-Control-Allow-Origin")
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Access-Control-Allow-Origin")

    assert call("OPTIONS", "https://hirerizz.vercel.app") == (204, "https://hirerizz.vercel.app")
    assert call("GET", "https://hirerizz.vercel.app") == (200, "https://hirerizz.vercel.app")
    assert call("OPTIONS", "https://hirerizz-git-main-soumyadip.vercel.app")[0] == 204  # Vercel preview URL
    assert call("OPTIONS", "https://evil.vercel.app")[0] == 403
    assert call("OPTIONS", "https://evil.example")[0] == 403
    assert call("GET", "https://evil.example") == (200, None)  # the browser won't let that site read it
    with urllib.request.urlopen(base + "/api/health") as r:
        h = json.loads(r.read())
        assert h["ok"] is True and "commit" in h
