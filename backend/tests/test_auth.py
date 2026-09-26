"""Dashboard login: the API needs a signed token; interview pages and health don't."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer

import pytest

from screening.dashboard import auth
from screening.dashboard.server import DashboardAPI, Handler


@pytest.fixture
def base(ctx, monkeypatch):
    monkeypatch.setenv("DASHBOARD_AUTH", "on")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, api=DashboardAPI(ctx.config, ctx=ctx)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(url, body=None, token=None):
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_api_needs_a_login(base, monkeypatch):
    monkeypatch.setattr("screening.dashboard.server.time.sleep", lambda s: None)
    assert call(base + "/api/jobs")[0] == 401
    assert call(base + "/api/approve/final", {"decisions": {}})[0] == 401
    assert call(base + "/api/health")[0] == 200                                   # public
    assert call(base + "/api/login", {"username": "admin", "password": "nope"})[0] == 401

    status, body = call(base + "/api/login", {"username": "Admin", "password": auth.DEFAULT_PASSWORD})
    assert status == 200 and body["user"] == "admin"
    tok = body["token"]
    assert call(base + "/api/me", token=tok) == (200, {"user": "admin", "login": True})
    assert call(base + "/api/jobs", token=tok)[0] == 200
    assert call(base + "/api/jobs", token=tok[:-2] + "xx")[0] == 401             # tampered
    assert call(base + "/api/jobs", token="garbage")[0] == 401


def test_tokens_expire_and_follow_the_password(monkeypatch):
    tok = auth.make_token("admin", now=1000)
    assert auth.verify_token(tok, now=1001) == "admin"
    assert auth.verify_token(tok, now=1000 + auth.TOKEN_TTL_SECONDS + 1) is None
    monkeypatch.setenv("DASHBOARD_PASSWORD", "a-new-password")
    assert auth.verify_token(tok, now=1001) is None                               # password changed: signed out
    assert auth.check_login("admin", "a-new-password") == "admin"
    assert auth.check_login("admin", auth.DEFAULT_PASSWORD) is None
