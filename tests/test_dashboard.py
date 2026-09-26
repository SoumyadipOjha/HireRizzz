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
