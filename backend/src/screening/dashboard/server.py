"""Web server: HR dashboard + candidate interview pages.

    screening serve            -> http://127.0.0.1:8765

* `/`                         HR dashboard (read-only). Loopback clients ONLY,
                              even if the server is bound to another interface.
* `/interview/<token>`        Candidate screening call (browser voice or text).
                              Only an unguessable token identifies the invite.
"""

from __future__ import annotations

import ipaddress
import os
import json
import re
import threading
import time
import webbrowser
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..agent.invites import TOKEN_RE, InviteError, InviteStore
from ..agent.verification import VerificationError
from . import auth
from ..config import AppConfig, ConfigError
from ..docx_reader import DocxReadError
from ..paths import PROJECT_ROOT
from ..llm.base import LLMError
from ..schemas import STAGES
from ..stage3_call import answers_for

def _frontend_dir() -> Path:
    """The static pages (../frontend next to backend/). In production they are hosted separately
    (Vercel) and this server is API-only; locally it serves them too, from the same address."""
    env = os.environ.get("HIRERIZZ_FRONTEND_DIR", "").strip()
    return Path(env) if env else PROJECT_ROOT.parent / "frontend"


def _static_root() -> Path:
    """The built React app (frontend/dist, `npm run build`) if there is one, else frontend/public
    (the interview page works without a build; the dashboard needs one)."""
    d = _frontend_dir()
    return d / "dist" if (d / "dist" / "index.html").is_file() else d / "public"


_TYPES = {".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".html": "text/html; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
          ".ico": "image/x-icon", ".json": "application/json; charset=utf-8", ".woff2": "font/woff2"}


def cors_origins() -> set[str]:
    """Sites allowed to call this API from a browser, e.g. the Vercel frontend (CORS_ORIGINS, comma-separated)."""
    return {o.strip().rstrip("/") for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()}


def origin_allowed(origin: str) -> bool:
    """Exact match, or a pattern with * (e.g. https://hire-rizzz-*.vercel.app for Vercel preview URLs)."""
    import fnmatch

    origin = origin.rstrip("/")
    return any(origin == o or ("*" in o and fnmatch.fnmatchcase(origin, o)) for o in cors_origins())


def dashboard_public() -> bool:
    """DASHBOARD_PUBLIC=true: the manager dashboard answers anyone, not just this computer. There is
    no login, so anyone with the URL can see candidates and approve/reject: the owner's choice."""
    return os.environ.get("DASHBOARD_PUBLIC", "").strip().lower() in ("1", "true", "yes")
DEMO_MARKER = "DEMO_DATA.txt"
MAX_BODY = 16 * 1024
MAX_UPLOAD_BODY = 30 * 1024 * 1024   # resume upload (base64 JSON): a few files of up to 10 MB
MAX_RESUME_BYTES = 10 * 1024 * 1024
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_LINKEDIN_API = re.compile(r"^/api/candidate/([0-9a-f-]{36})/linkedin$")
_CLEAR_FRAUD_API = re.compile(r"^/api/candidate/([0-9a-f-]{36})/clear-fraud$")
_CLARIFY_API = re.compile(r"^/api/candidate/([0-9a-f-]{36})/request-clarification$")
_JOB_API = re.compile(r"^/api/jobs/([A-Za-z0-9_-]{1,80})$")
_JOB_STATUS_API = re.compile(r"^/api/jobs/([A-Za-z0-9_-]{1,80})/status$")
_INTERVIEW_API = re.compile(r"^/api/interview/([A-Za-z0-9_-]+)/(info|code|verify|start|turn|end)$")
_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


class DashboardAPI:
    def __init__(self, config: AppConfig, ctx=None, mailer_factory=None, llm_factory=None):
        self.config = config
        self.store = config.store
        self.invites = InviteStore(self.store)
        self._ctx = ctx
        self._mailer_factory = mailer_factory
        self._llm_factory = llm_factory
        self.processing = {"running": False, "message": None}  # resume upload -> screening in the background

    @property
    def ctx(self):
        if self._ctx is None:
            from ..context import RunContext

            self._ctx = RunContext.create(self.config)
        return self._ctx

    def _mailer(self):
        if self._mailer_factory is None:
            return None  # approvals.py builds one from settings (and handles "not configured")
        return self._mailer_factory()

    # -------------------------------------------------------------- approvals (loopback only)

    def approve(self, gate: str, body: dict) -> dict:
        from ..approvals import approve_final, approve_shortlist

        decisions = body.get("decisions")
        if not isinstance(decisions, dict) or not decisions or len(decisions) > 1000:
            raise ValueError("decisions must be an object of candidate_id -> shortlisted|rejected")
        if not all(isinstance(k, str) and _UUID.match(k) for k in decisions):
            raise ValueError("decisions contains an invalid candidate_id")
        note = body.get("note") if isinstance(body.get("note"), str) else None
        by = body.get("by") if isinstance(body.get("by"), str) else ""
        with self.ctx.lock:
            if gate == "shortlist":
                return approve_shortlist(self.ctx, decisions, by=by, note=note, mailer=self._mailer())
            return approve_final(self.ctx, decisions, by=by, note=note)

    # -------------------------------------------------------------- resume upload (loopback only)

    def upload_resumes(self, body: dict) -> dict:
        """Save uploaded .docx/.pdf resumes to the input folder, then screen them (Stages 0-2)
        in the background. The dashboard shows progress through overview()["processing"]."""
        import base64
        import binascii

        from ..resume_reader import SUPPORTED

        from ..stage0_ingest import resumes_dir

        files = body.get("files")
        if not isinstance(files, list) or not files or len(files) > 20:
            raise ValueError("files must be a list of 1-20 {name, data} objects")
        job_id = self._job_id(body.get("job_id"))
        if self.config.jobs.get(job_id).status == "closed":
            raise ValueError("this job is closed: reopen it to add resumes")
        folder = resumes_dir(self.ctx, job_id)
        folder.mkdir(parents=True, exist_ok=True)
        saved = []
        for f in files:
            name = f.get("name") if isinstance(f, dict) else None
            if not isinstance(name, str) or not isinstance(f.get("data"), str):
                raise ValueError("each file needs a name and base64 data")
            stem, suffix = Path(name).stem, Path(name).suffix.lower()
            if suffix not in SUPPORTED:
                raise ValueError(f"{name}: only {', '.join(SUPPORTED)} files are accepted")
            try:
                data = base64.b64decode(f["data"], validate=True)
            except (binascii.Error, ValueError):
                raise ValueError(f"{name}: file data is not valid base64") from None
            if not data or len(data) > MAX_RESUME_BYTES:
                raise ValueError(f"{name}: file is empty or larger than 10 MB")
            safe = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:60] or "resume"
            target, i = folder / f"{safe}{suffix}", 1
            while target.exists() and target.read_bytes() != data:
                i += 1
                target = folder / f"{safe}_{i}{suffix}"
            target.write_bytes(data)
            saved.append(target.name)
        if not self.processing["running"]:
            self.processing.update(running=True, message=f"Screening {len(saved)} resume(s)…", job_id=job_id)
            threading.Thread(target=self._screen_uploads, args=(job_id,), name="screen-uploads", daemon=True).start()
        return {"saved": saved, "job_id": job_id}

    def _job_id(self, value) -> str:
        """A job id from a request (missing = the default job). Unknown job -> KeyError (404)."""
        from ..jobs import JobError

        if value is not None and not isinstance(value, str):
            raise ValueError("job_id must be a string")
        try:
            return self.config.jobs.get(value or None).job["job_id"]
        except JobError as e:
            raise KeyError(str(e)) from None

    def _screen_uploads(self, job_id: str | None = None) -> None:
        from ..llm import make_client
        from ..stage0_ingest import ingest
        from ..stage1_extract import run_stage1
        from ..stage2_shortlist import run_stage2

        try:
            llm = self._llm_factory() if self._llm_factory else make_client(self.config)
            with self.ctx.lock:
                ingest(self.ctx, job_id=job_id)
                s1 = run_stage1(self.ctx, llm)
                s2 = run_stage2(self.ctx, llm)
            failed = len(s1.failed) + len(s2.failed)
            self.processing.update(message=f"Done: {len(s2.succeeded)} scored" + (f", {failed} failed" if failed else ""))
        except Exception as e:  # e.g. no API key: show it on the dashboard
            self.ctx.logger.error("upload screening failed: %s: %s", type(e).__name__, e)
            self.processing.update(message=f"Screening failed: {e}")
        finally:
            self.processing["running"] = False

    # -------------------------------------------------------------- JD writer (gate 1, loopback only)

    def jd_draft(self) -> dict:
        from ..jd_writer import load_draft

        return {"draft": load_draft(self.config)}

    def write_jd(self, body: dict) -> dict:
        from ..jd_writer import draft_jd
        from ..llm import make_client

        brief = body.get("brief") if isinstance(body.get("brief"), str) else ""
        company = body.get("company_name") if isinstance(body.get("company_name"), str) else None
        llm = self._llm_factory() if self._llm_factory else make_client(self.config)
        return {"draft": draft_jd(self.config, llm, brief, company_name=company)}

    def approve_jd(self, body: dict) -> dict:
        """Post the (edited) draft as a new job, or update an existing one (body.job_id)."""
        from ..jd_writer import JDError, approve_jd

        job, questions = body.get("job"), body.get("questions")
        if not isinstance(job, dict) or not isinstance(questions, list):
            raise JDError("job (object) and questions (list) are required")
        update = self._job_id(body["job_id"]) if body.get("job_id") else None
        with self.ctx.lock:
            jobs = self.config.jobs
            scored = sum(e.stages["stage2_shortlisting"].status == "success" and jobs.candidate_job(e) == update
                         for e in self.ctx.index.reload().all()) if update else 0
            return approve_jd(self.config, job, questions, by=body.get("by") if isinstance(body.get("by"), str) else "",
                              scored_candidates=scored, job_id=update)

    # -------------------------------------------------------------- jobs

    def _entries(self, job_id: str | None = None) -> list[dict]:
        """Index entries (as dicts), each with its job_id resolved and its board column."""
        from ..approvals import board_column
        from ..schemas import CandidateEntry

        default = self.config.jobs.default_id()
        out = []
        for raw in (self._index().get("candidates") or {}).values():
            try:
                entry = CandidateEntry.model_validate(raw)
            except ValueError:
                continue
            jid = entry.job_id or default
            if job_id is not None and jid != job_id:
                continue
            out.append({**raw, "job_id": jid, "board_column": board_column(entry)})
        return out

    def _job_summary(self, rec, entries: list[dict]) -> dict:
        from ..approvals import BOARD_COLUMNS

        counts = {c: 0 for c in BOARD_COLUMNS}
        for e in entries:
            counts[e["board_column"]] += 1
        j = rec.job
        return {"job_id": j["job_id"], "title": j["title"], "company_name": j["company_name"],
                "location": j.get("location"), "status": rec.status, "created_at": rec.created_at,
                "posted_by": rec.posted_by or j.get("approved_by"), "candidate_count": len(entries),
                "counts": counts, "must_have_skills": j.get("must_have_skills", []),
                "last_activity": max((e.get("updated_at") or "" for e in entries), default=None) or None}

    def jobs(self) -> dict:
        from ..approvals import BOARD_COLUMNS

        self.config.jobs.refresh()
        by_job: dict[str, list[dict]] = {}
        for e in self._entries():
            by_job.setdefault(e["job_id"], []).append(e)
        return {"jobs": [self._job_summary(r, by_job.get(r.job["job_id"], [])) for r in self.config.jobs.all()],
                "columns": list(BOARD_COLUMNS)}

    def job(self, job_id: str) -> dict:
        from ..approvals import BOARD_COLUMNS

        job_id = self._job_id(job_id)
        rec = self.config.jobs.get(job_id)
        entries = self._entries(job_id)
        return {**self._job_summary(rec, entries), "job": rec.job, "questions": rec.questions,
                "columns": list(BOARD_COLUMNS), "candidates": entries, "is_default": rec.is_default}

    def set_job_status(self, job_id: str, body: dict) -> dict:
        status = body.get("status")
        if status not in ("open", "closed"):
            raise ValueError("status must be open or closed")
        rec = self.config.jobs.set_status(self._job_id(job_id), status)
        return {"job_id": rec.job["job_id"], "status": rec.status}

    def send_results(self, body: dict | None = None) -> dict:
        from ..approvals import send_final_results

        job_id = self._job_id(body["job_id"]) if body and body.get("job_id") else None
        with self.ctx.lock:
            only = {e["candidate_id"] for e in self._entries(job_id)} if job_id else None
            return {"emails": send_final_results(self.ctx, self._mailer(), only=only)}

    def results(self, job_id: str | None = None) -> dict:
        from ..approvals import final_results

        with self.ctx.lock:
            return final_results(self.ctx, self._job_id(job_id) if job_id else None)

    def results_csv(self, job_id: str | None = None) -> str:
        from ..approvals import results_csv

        return results_csv(self.results(job_id))

    def _index(self) -> dict:
        try:
            return self.store.load_index() or {"candidates": {}, "updated_at": None}
        except ValueError:  # a half-written/corrupt index file: show an empty page, not an error
            return {"candidates": {}, "updated_at": None}

    def _failures(self) -> list[dict]:
        return self.store.failures()[::-1]  # newest first

    def overview(self, job_id: str | None = None) -> dict:
        """Settings, and the candidates (all jobs, or one job's with ?job=)."""
        cfg, data = self.config, self.config.data
        job_error = None
        index = self._index()
        try:
            jid = self._job_id(job_id)
            job = cfg.for_job(jid).job.model_dump()
            index = {**index, "candidates": {e["candidate_id"]: e for e in self._entries(jid if job_id else None)}}
        except KeyError:
            if job_id:
                raise
            job, job_error = None, "no job posted yet"
        except Exception as e:  # show config problems on the page instead of a blank screen
            job, job_error = None, str(e)
        try:
            cfg.api_key()
            llm_error = None
        except ConfigError as e:
            llm_error = str(e)
        return {
            "index": index,
            "llm_error": llm_error,
            "job": job,
            "job_error": job_error,
            "thresholds": cfg.settings.thresholds.model_dump(),
            "evaluation": cfg.settings.evaluation.model_dump(),
            "approvals": cfg.settings.approvals.model_dump(),
            "email_mode": cfg.settings.email.mode,
            "weights": cfg.settings.scoring.weights,
            "llm": {"provider": cfg.settings.llm.provider, "model": cfg.settings.llm.model},
            "data_dir": data.root.as_posix(),
            "storage": self.store.describe(),
            "demo": (data.root / DEMO_MARKER).exists(),
            "stages": list(STAGES),
            "failure_count": len(self._failures()),
            "processing": dict(self.processing),
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
        answers = answers_for(self.config, s3) if s3 else None
        try:
            from ..approvals import board_column
            from ..schemas import CandidateEntry

            entry = {**entry, "job_id": entry.get("job_id") or self.config.jobs.default_id(),
                     "board_column": board_column(CandidateEntry.model_validate(entry))}
        except ValueError:
            pass
        credibility = self._credibility(cid, entry)
        try:
            inv = self.invites.active_for(cid)
        except (OSError, ValueError):
            inv = None
        invite = {"url": interview_url(self.config, inv.token), "expires_at": inv.expires_at,
                  "sessions": len(inv.sessions), "email_to": inv.email_to, "emailed_at": inv.emailed_at,
                  "email_error": inv.email_error, "reminders_sent": inv.reminders_sent, "opened_at": inv.opened_at,
                  "verified_at": inv.verified_at} if inv else None
        return {"entry": entry, "records": records, "transcript": transcript, "answers": answers,
                "credibility": credibility, "invite": invite,
                "failures": [f for f in self._failures() if f.get("candidate_id") == cid]}

    def failures(self) -> list[dict]:
        return self._failures()

    # -------------------------------------------------------------- resume credibility (loopback only)

    def _credibility(self, cid: str, entry: dict) -> dict | None:
        from ..credibility import check_resume, load_record

        rec = load_record(self.config, cid)
        if rec is None and (entry.get("stages", {}).get("stage1_extraction") or {}).get("status") == "success":
            try:  # candidates screened before the checks existed: run them now (code only, instant)
                with self.ctx.lock:
                    self.ctx.index.reload()
                    rec = check_resume(self.ctx, cid)
            except Exception as e:
                self.ctx.logger.warning("credibility: candidate_id=%s checks failed: %s", cid, e)
        return rec.model_dump(mode="json") if rec else None

    def request_clarification(self, cid: str) -> dict:
        """Email the candidate the mismatches and ask for an updated resume (manual send / resend)."""
        from ..credibility import request_clarification

        with self.ctx.lock:
            self.ctx.index.reload().get(cid)
            n = request_clarification(self.ctx, cid, mailer=self._mailer())
        return n.model_dump(mode="json")

    def clear_fraud(self, cid: str, body: dict) -> dict:
        """A recruiter reviewed the flags: the candidate continues (resume scoring runs in the background)."""
        from ..credibility import clear_fraud

        by = body.get("by") if isinstance(body.get("by"), str) else ""
        note = body.get("note") if isinstance(body.get("note"), str) else None
        with self.ctx.lock:
            entry = clear_fraud(self.ctx, self.ctx.index.reload().get(cid).candidate_id, by=by, note=note)
        if entry.stages["stage2_shortlisting"].status != "success" and not self.processing["running"]:
            self.processing.update(running=True, message=f"Scoring {entry.display_name or 'the resume'} after clearance…")
            threading.Thread(target=self._continue_after_clearance, args=(cid,), daemon=True).start()
        return {"cleared": True, "overall_status": entry.overall_status}

    def _continue_after_clearance(self, cid: str) -> None:
        from ..llm import make_client
        from ..stage2_shortlist import run_stage2

        try:
            llm = self._llm_factory() if self._llm_factory else make_client(self.config)
            with self.ctx.lock:
                self.ctx.index.reload()
                s2 = run_stage2(self.ctx, llm, only={cid})
            self.processing.update(message="Done: resume scored" if s2.succeeded else "Scoring failed: see Failures")
        except Exception as e:
            self.ctx.logger.error("clearance: scoring failed: %s: %s", type(e).__name__, e)
            self.processing.update(message=f"Scoring failed: {e}")
        finally:
            self.processing["running"] = False

    def upload_linkedin(self, cid: str, body: dict) -> dict:
        """The candidate's LinkedIn 'Save to PDF' export -> compared with their resume."""
        import base64
        import binascii

        from ..credibility import check_linkedin
        from ..llm import make_client

        name, data = body.get("name"), body.get("data")
        if not isinstance(name, str) or not name.lower().endswith(".pdf") or not isinstance(data, str):
            raise ValueError("send the LinkedIn profile as a .pdf (LinkedIn -> profile -> More -> Save to PDF)")
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("file data is not valid base64") from None
        if not raw.startswith(b"%PDF") or len(raw) > MAX_RESUME_BYTES:
            raise ValueError("that isn't a PDF, or it's larger than 10 MB")
        with self.ctx.lock:
            entry = self.ctx.index.reload().get(cid)  # KeyError -> 404
            if entry.stages["stage1_extraction"].status != "success":
                raise ValueError("read the resume first (Stage 1), then compare it with LinkedIn")
        folder = self.config.data.root / "input" / "linkedin"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{cid}.pdf"
        path.write_bytes(raw)
        llm = self._llm_factory() if self._llm_factory else make_client(self.config)
        with self.ctx.lock:
            self.ctx.index.reload()
            rec = check_linkedin(self.ctx, llm, cid, path, Path(name).name[:120])
        return rec.model_dump(mode="json")

    def all_answers(self, job_id: str | None = None) -> list[dict]:
        """Every candidate who has had a screening call (all jobs, or one), with their answers."""
        out = []
        for e in self._entries(self._job_id(job_id) if job_id else None):
            cid = e["candidate_id"]
            ref = (e.get("stages", {}).get("stage3_calling") or {}).get("output_path")
            s3 = self.store.get_record(ref) if ref else None
            if not s3:
                continue
            s4 = e.get("stages", {}).get("stage4_evaluation") or {}
            final = (e.get("reviews") or {}).get("final") or {}
            out.append({"candidate_id": cid, "name": e.get("display_name"), "call": s3.get("call"),
                        "summary": (s3.get("screening") or {}).get("overall_summary"),
                        "final_score": s4.get("score"), "ai_suggestion": s4.get("decision"),
                        "decision": final.get("decision"), "answers": answers_for(self.config, s3)})
        out.sort(key=lambda r: ((r["call"] or {}).get("ended_at") or ""), reverse=True)
        return out

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

    def _dashboard_allowed(self) -> bool:
        return dashboard_public() or self._is_loopback()

    def _session_user(self) -> str | None:
        """Who is signed in (Authorization: Bearer <token>). With the login off: "" (anyone)."""
        if not auth.auth_enabled():
            return ""
        h = self.headers.get("Authorization") or ""
        return auth.verify_token(h[7:].strip()) if h[:7].lower() == "bearer " else None

    def _login(self) -> None:
        if not self._same_origin_json():
            return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        body = self._body()
        user = auth.check_login(body.get("username"), body.get("password"))
        if user is None:
            time.sleep(1)  # slows down password guessing
            return self._json({"error": "Wrong username or password."}, HTTPStatus.UNAUTHORIZED)
        return self._json({"token": auth.make_token(user), "user": user})

    def _allowed_origin(self) -> str | None:
        origin = (self.headers.get("Origin") or "").rstrip("/")
        return origin if origin and origin_allowed(origin) else None

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
        if origin := self._allowed_origin():  # the separately hosted frontend
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _page(self, name: str, extra: dict | None = None) -> None:
        f = _static_root() / name
        if not f.is_file():  # API-only deployment: the pages live on the frontend host
            return self._json({"error": "This is the HireRizz API. Open the frontend site instead."},
                              HTTPStatus.NOT_FOUND)
        self._send(200, f.read_bytes(), _TYPES.get(f.suffix.lower(), "application/octet-stream"), extra)

    def _spa_fallback(self, path: str) -> None:
        """Files of the built frontend (/assets/...), and index.html for the app's own routes (/jobs/<id>)."""
        if path.startswith("/api/"):
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        root = _static_root().resolve()
        rel = path.lstrip("/")
        if rel and "." in rel.rsplit("/", 1)[-1]:
            f = (root / rel).resolve()
            if root in f.parents and f.is_file():
                return self._page(f.relative_to(root).as_posix())
        elif path == "/jobs" or path.startswith("/jobs/"):
            return self._page("index.html")
        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _body(self, limit: int = MAX_BODY) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
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
            if path == "/config.js":
                return self._page("config.js")
            if path == "/api/health":
                # Render sets RENDER_GIT_COMMIT: shows which commit is deployed
                commit = os.environ.get("RENDER_GIT_COMMIT", "")[:7] or None
                return self._json({"ok": True, "service": "hirerizz-backend", "commit": commit})
            if m := _INTERVIEW_API.match(path):
                return self._interview(m.group(1), m.group(2), method="GET")

            # ---- everything below is the HR dashboard: this computer only, unless DASHBOARD_PUBLIC ----
            if not self._dashboard_allowed():
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            if path in ("/", "/index.html"):
                return self._page("index.html")
            if path.startswith("/api/"):  # the app's pages load freely (they show the login); the data doesn't
                user = self._session_user()
                if user is None:
                    return self._json({"error": "Please sign in."}, HTTPStatus.UNAUTHORIZED)
                if path == "/api/me":
                    return self._json({"user": user or None, "login": auth.auth_enabled()})
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            job_q = q.get("job") or None
            if path == "/api/overview":
                return self._json(self.api.overview(job_q))
            if path == "/api/jobs":
                return self._json(self.api.jobs())
            if m := _JOB_API.match(path):
                return self._json(self.api.job(m.group(1)))
            if path.startswith("/api/candidate/"):
                cid = path.rsplit("/", 1)[-1]
                if not _UUID.match(cid):
                    return self._json({"error": "invalid candidate_id"}, HTTPStatus.BAD_REQUEST)
                data = self.api.candidate(cid)
                return self._json(data) if data else self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            if path == "/api/failures":
                return self._json(self.api.failures())
            if path == "/api/results":
                return self._json(self.api.results(job_q))
            if path == "/api/answers":
                return self._json(self.api.all_answers(job_q))
            if path == "/api/jd/draft":
                return self._json(self.api.jd_draft())
            if path == "/api/results.csv":
                body = self.api.results_csv(job_q).encode("utf-8-sig")  # BOM: Excel opens UTF-8 names correctly
                return self._send(200, body, "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{self._csv_name(job_q)}"'})
            if path == "/api/log":
                n = int(parse_qs(url.query).get("lines", ["300"])[0])
                return self._json({"log": self.api.log_tail(max(1, min(n, 5000)))})
            return self._spa_fallback(path)
        except KeyError as e:  # unknown job
            return self._json({"error": str(e).strip("'\"")}, HTTPStatus.NOT_FOUND)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            if m := _INTERVIEW_API.match(path):
                return self._interview(m.group(1), m.group(2), method="POST")
            if path == "/api/login":
                if not self._dashboard_allowed():
                    return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return self._login()
            if path in ("/api/approve/shortlist", "/api/approve/final", "/api/send-results", "/api/jd/draft",
                        "/api/jd/approve", "/api/jobs", "/api/resumes") or _LINKEDIN_API.match(path) \
                    or _CLEAR_FRAUD_API.match(path) or _CLARIFY_API.match(path) or _JOB_STATUS_API.match(path):
                return self._dashboard_action(path)
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_OPTIONS(self):  # noqa: N802
        """CORS preflight from the separately hosted frontend."""
        if not self._allowed_origin():
            return self._json({"error": "origin not allowed"}, HTTPStatus.FORBIDDEN)
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", self._allowed_origin())
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _same_origin_json(self) -> bool:
        """Dashboard actions change data and send email: refuse cross-site requests. A page on
        another site can make the browser POST here, but not with a JSON content type (that
        needs a CORS preflight this server never grants), and its Origin header gives it away."""
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # non-browser clients (curl, tests)
        host = self.headers.get("Host", "")
        return origin in (f"http://{host}", f"https://{host}") or origin_allowed(origin)

    def _dashboard_action(self, path: str) -> None:
        from ..approvals import ApprovalError

        if not self._dashboard_allowed():
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        if not self._same_origin_json():
            return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        user = self._session_user()
        if user is None:
            return self._json({"error": "Please sign in."}, HTTPStatus.UNAUTHORIZED)
        try:
            body = self._body(MAX_UPLOAD_BODY if path == "/api/resumes" or _LINKEDIN_API.match(path) else MAX_BODY)
            if user:  # decisions are recorded under the signed-in account, whatever the page sends
                body["by"] = user
            if m := _LINKEDIN_API.match(path):
                return self._json(self.api.upload_linkedin(m.group(1), body))
            if m := _CLEAR_FRAUD_API.match(path):
                return self._json(self.api.clear_fraud(m.group(1), body))
            if m := _CLARIFY_API.match(path):
                return self._json(self.api.request_clarification(m.group(1)))
            if m := _JOB_STATUS_API.match(path):
                return self._json(self.api.set_job_status(m.group(1), body))
            if path == "/api/resumes":
                return self._json(self.api.upload_resumes(body))
            if path == "/api/send-results":
                return self._json(self.api.send_results(body))
            if path == "/api/jd/draft":
                return self._json(self.api.write_jd(body))
            if path in ("/api/jd/approve", "/api/jobs"):  # post a job (the JD writer's approved draft)
                return self._json(self.api.approve_jd(body))
            return self._json(self.api.approve(path.rsplit("/", 1)[-1], body))
        except (ApprovalError, ValueError, DocxReadError) as e:  # DocxReadError: unreadable PDF/resume
            return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        except ConfigError as e:  # e.g. no Gemini key for the JD writer
            return self._json({"error": str(e)}, HTTPStatus.SERVICE_UNAVAILABLE)
        except LLMError as e:
            return self._json({"error": f"The AI couldn't draft this right now: {e}"}, HTTPStatus.BAD_GATEWAY)
        except KeyError as e:
            return self._json({"error": str(e).strip("'\"")}, HTTPStatus.NOT_FOUND)

    def _csv_name(self, job_id: str | None = None) -> str:
        job_id = re.sub(r"[^A-Za-z0-9_-]+", "-", job_id or "all-jobs")[:60] or "results"
        return f"final-results-{job_id}.csv"

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
    httpd = ThreadingHTTPServer((host, port), partial(Handler, api=DashboardAPI(config, ctx=ctx, llm_factory=lambda: make_client(config)), interviews=interviews))
    url = f"http://127.0.0.1:{port}/"
    print(f"Dashboard:  {url}   (storage: {config.store.describe()})", flush=True)
    print(f"Interviews: {config.settings.stage3.public_base_url}/interview/<token>   — Ctrl+C to stop", flush=True)
    if dashboard_public():
        print("WARNING: DASHBOARD_PUBLIC is on. The manager dashboard has no login: anyone with its URL can "
              "see candidates and approve or reject them.", flush=True)
    if cors_origins():
        print(f"Frontend:   {', '.join(sorted(cors_origins()))} (CORS)", flush=True)
    if host not in ("127.0.0.1", "localhost", "::1") and not dashboard_public():
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
