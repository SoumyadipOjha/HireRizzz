"""Web server: HR dashboard + candidate interview pages.

    screening serve            -> http://127.0.0.1:8765

* `/`                         HR dashboard (read-only). Loopback clients ONLY,
                              even if the server is bound to another interface.
* `/interview/<token>`        Candidate screening call (browser voice or text).
                              Only an unguessable token identifies the invite.
"""

from __future__ import annotations

import ipaddress
import json
import re
import threading
import webbrowser
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..agent.invites import TOKEN_RE, InviteError, InviteStore
from ..agent.verification import VerificationError
from ..config import AppConfig, ConfigError
from ..schemas import STAGES

STATIC = Path(__file__).parent / "static"
DEMO_MARKER = "DEMO_DATA.txt"
MAX_BODY = 16 * 1024
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_INTERVIEW_API = re.compile(r"^/api/interview/([A-Za-z0-9_-]+)/(info|code|verify|start|turn|end)$")
_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


class DashboardAPI:
    def __init__(self, config: AppConfig):
        self.config = config
        self.store = config.store
        self.invites = InviteStore(self.store)

    def _index(self) -> dict:
        try:
            return self.store.load_index() or {"candidates": {}, "updated_at": None}
        except ValueError:  # a half-written/corrupt index file: show an empty page, not an error
            return {"candidates": {}, "updated_at": None}

    def _failures(self) -> list[dict]:
        return self.store.failures()[::-1]  # newest first

    def overview(self) -> dict:
        cfg, data = self.config, self.config.data
        job_error = None
        try:
            job = cfg.job.model_dump()
        except Exception as e:  # show config problems on the page instead of a blank screen
            job, job_error = None, str(e)
        try:
            cfg.api_key()
            llm_error = None
        except ConfigError as e:
            llm_error = str(e)
        return {
            "index": self._index(),
            "llm_error": llm_error,
            "job": job,
            "job_error": job_error,
            "thresholds": cfg.settings.thresholds.model_dump(),
            "weights": cfg.settings.scoring.weights,
            "llm": {"provider": cfg.settings.llm.provider, "model": cfg.settings.llm.model},
            "data_dir": data.root.as_posix(),
            "storage": self.store.describe(),
            "demo": (data.root / DEMO_MARKER).exists(),
            "stages": list(STAGES),
            "failure_count": len(self._failures()),
        }

    def candidate(self, cid: str) -> dict | None:
        from ..stage3_call import interview_url

        entry = self._index().get("candidates", {}).get(cid)
        if entry is None:
            return None
        records = {}
        for stage in STAGES:
            out = entry["stages"].get(stage, {}).get("output_path")
            records[stage] = self.store.get_record(out) if out else None
        transcript = None
        s3 = records.get("stage3_calling")
        if s3 and s3.get("transcript_path"):
            transcript = self.store.get_text(s3["transcript_path"])
        try:
            inv = self.invites.active_for(cid)
        except (OSError, ValueError):
            inv = None
        invite = {"url": interview_url(self.config, inv.token), "expires_at": inv.expires_at,
                  "sessions": len(inv.sessions)} if inv else None
        return {"entry": entry, "records": records, "transcript": transcript, "invite": invite,
                "failures": [f for f in self._failures() if f.get("candidate_id") == cid]}

    def failures(self) -> list[dict]:
        return self._failures()

    def log_tail(self, lines: int) -> str:
        p = self.config.data.pipeline_log
        if not p.exists():
            return ""
        return "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


class Handler(BaseHTTPRequestHandler):
    server_version = "ScreeningServer/1.0"

    def __init__(self, *args, api: DashboardAPI, interviews=None, **kwargs):
        self.api = api
        self.interviews = interviews  # InterviewService | None
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # -------------------------------------------------------------- helpers

    def _is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0].split("%")[0]).is_loopback
        except ValueError:
            return False

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")  # interview URLs carry a secret token
        self.send_header("Content-Security-Policy", _CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _page(self, name: str, extra: dict | None = None) -> None:
        self._send(200, (STATIC / name).read_bytes(), "text/html; charset=utf-8", extra)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("request too large")
        raw = self.rfile.read(n) if n else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("JSON object expected")
        return data

    # -------------------------------------------------------------- routes

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        try:
            if path.startswith("/interview/"):
                return self._page("interview.html", {"Permissions-Policy": "microphone=(self), camera=()"})
            if m := _INTERVIEW_API.match(path):
                return self._interview(m.group(1), m.group(2), method="GET")

            # ---- everything below is the HR dashboard: loopback only ----
            if not self._is_loopback():
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            if path in ("/", "/index.html"):
                return self._page("index.html")
            if path == "/api/overview":
                return self._json(self.api.overview())
            if path.startswith("/api/candidate/"):
                cid = path.rsplit("/", 1)[-1]
                if not _UUID.match(cid):
                    return self._json({"error": "invalid candidate_id"}, HTTPStatus.BAD_REQUEST)
                data = self.api.candidate(cid)
                return self._json(data) if data else self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            if path == "/api/failures":
                return self._json(self.api.failures())
            if path == "/api/log":
                n = int(parse_qs(url.query).get("lines", ["300"])[0])
                return self._json({"log": self.api.log_tail(max(1, min(n, 5000)))})
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            if m := _INTERVIEW_API.match(path):
                return self._interview(m.group(1), m.group(2), method="POST")
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _interview(self, token: str, action: str, method: str) -> None:
        if not TOKEN_RE.match(token):
            return self._json({"error": "invalid link"}, HTTPStatus.NOT_FOUND)
        if self.interviews is None:
            return self._json({"error": "Interviews are not available on this server."}, HTTPStatus.SERVICE_UNAVAILABLE)
        svc = self.interviews
        try:
            if action == "info" and method == "GET":
                return self._json(svc.info(token))
            if method != "POST":
                return self._json({"error": "method not allowed"}, HTTPStatus.METHOD_NOT_ALLOWED)
            body = self._body()
            if action == "code":
                return self._json(svc.request_code(token))
            if action == "verify":
                code = body.get("code")
                if not isinstance(code, str) or len(code) > 20:
                    return self._json({"error": "code must be a short string"}, HTTPStatus.BAD_REQUEST)
                return self._json(svc.verify_code(token, code))
            if action == "start":
                channel = body.get("channel")
                if channel not in ("browser_voice", "browser_text"):
                    return self._json({"error": "channel must be browser_voice or browser_text"}, HTTPStatus.BAD_REQUEST)
                key = body.get("access_key")
                return self._json(svc.start(token, channel, key if isinstance(key, str) else None))
            sid = str(body.get("session_id") or "")
            if not _UUID.match(sid):
                return self._json({"error": "invalid session_id"}, HTTPStatus.BAD_REQUEST)
            if action == "turn":
                text = body.get("text")
                if not isinstance(text, str):
                    return self._json({"error": "text must be a string"}, HTTPStatus.BAD_REQUEST)
                return self._json(svc.turn(token, sid, text))
            return self._json(svc.hang_up(token, sid))  # action == "end"
        except InviteError as e:
            return self._json({"error": str(e)}, HTTPStatus.GONE)
        except VerificationError as e:
            return self._json({"error": str(e)}, e.status)
        except ConfigError as e:  # e.g. GEMINI_API_KEY or SMTP settings missing
            svc.ctx.logger.error("stage3 agent: cannot serve %s: %s", action, e)
            return self._json({"error": "The screening assistant is not available right now. "
                                        "Please try again later."}, HTTPStatus.SERVICE_UNAVAILABLE)
        except ValueError as e:
            return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)


def serve(config: AppConfig, port: int = 8765, open_browser: bool = True, host: str = "127.0.0.1") -> None:
    from ..agent.service import InterviewService
    from ..context import RunContext
    from ..llm import make_client

    ctx = RunContext.create(config)
    interviews = InterviewService(ctx, llm_factory=lambda: make_client(config))
    httpd = ThreadingHTTPServer((host, port), partial(Handler, api=DashboardAPI(config), interviews=interviews))
    url = f"http://127.0.0.1:{port}/"
    print(f"Dashboard:  {url}   (storage: {config.store.describe()})", flush=True)
    print(f"Interviews: {config.settings.stage3.public_base_url}/interview/<token>   — Ctrl+C to stop", flush=True)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"Listening on {host}:{port}. The dashboard still answers loopback clients only.", flush=True)
    try:
        config.api_key()
    except ConfigError as e:
        print(f"WARNING: {e}\n         The dashboard works, but interviews cannot start until the key is set.", flush=True)
    try:
        from ..mailer import make_mailer

        mailer = make_mailer(config)
        print(f"Email:      {mailer.mode}", flush=True)
    except ConfigError as e:
        mailer = None
        print(f"WARNING: {e}\n         Verification codes and reminders cannot be emailed until this is set.", flush=True)
    stop = threading.Event()
    if mailer is not None and config.settings.email.auto_reminders:
        threading.Thread(target=_reminder_loop, args=(ctx, mailer, stop), name="reminders", daemon=True).start()
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
        interviews.shutdown()


REMINDER_CHECK_SECONDS = 600


def _reminder_loop(ctx, mailer, stop: threading.Event) -> None:
    """Every 10 minutes, email candidates who haven't started their call (email.reminder_after_hours)."""
    from ..agent.notify import send_reminders

    while not stop.wait(REMINDER_CHECK_SECONDS):
        try:
            send_reminders(ctx, mailer)
        except Exception as e:  # never let a mail/DB hiccup kill the loop
            ctx.logger.error("reminders: check failed: %s: %s", type(e).__name__, e)
