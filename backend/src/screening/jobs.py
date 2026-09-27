"""Posted jobs. Each job has its own description, screening questions and candidates.

Jobs live in the store (MongoDB `jobs` collection / data/jobs/*.json). The first time
the store has no jobs, the YAML job (config/job_description.yaml +
config/screening_questions.yaml) is imported as the first job, and candidates that
predate jobs (no job_id) belong to it: the *default job*.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .schemas import utc_now

JobStatus = Literal["open", "closed"]


class JobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job: dict                              # JobDescription fields (validated on read)
    questions: list[dict]                  # ScreeningQuestion fields
    status: JobStatus = "open"
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    posted_by: str | None = None
    is_default: bool = False               # holds candidates that predate jobs
    deadline: str | None = None            # applications close (ISO date or date-time); shown as a countdown


class JobError(ValueError):
    """Unknown job, or an invalid job definition."""


def parse_deadline(value) -> str | None:
    """"" / None = no deadline; otherwise an ISO date ("2026-10-15") or date-time, stored as given."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise JobError("deadline must be a date like 2026-10-15")
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise JobError(f"deadline {value!r} is not a date like 2026-10-15") from None
    return value.strip()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "job"


class JobStore:
    def __init__(self, config):
        self.config = config
        self.store = config.store
        self._cache: dict[str, JobRecord] | None = None

    # -- read ---------------------------------------------------------------
    def _load(self) -> dict[str, JobRecord]:
        if self._cache is None:
            recs = {}
            for d in self.store.list_records("jobs"):
                r = JobRecord.model_validate(d)
                recs[r.job["job_id"]] = r
            if not recs:
                recs = self._import_yaml_job()
            self._cache = recs
        return self._cache

    def refresh(self) -> "JobStore":
        self._cache = None
        return self

    def all(self) -> list[JobRecord]:
        return sorted(self._load().values(), key=lambda r: r.created_at, reverse=True)

    def get(self, job_id: str | None) -> JobRecord:
        jobs = self._load()
        if not jobs:
            raise JobError("no job posted yet: post one in the dashboard, or add config/job_description.yaml")
        if job_id is None:
            return jobs[self.default_id()]
        if job_id not in jobs:
            self.refresh()
            jobs = self._load()
        try:
            return jobs[job_id]
        except KeyError:
            raise JobError(f"unknown job {job_id!r}") from None

    def default_id(self) -> str:
        jobs = self._load()
        if not jobs:
            raise JobError("no job posted yet: post one in the dashboard, or add config/job_description.yaml")
        d = next((j for j, r in jobs.items() if r.is_default), None)
        return d or min(jobs.items(), key=lambda kv: kv[1].created_at)[0]

    def stored_id(self, job_id: str | None) -> str | None:
        """How a job is written on candidates: None for the default job (as before jobs existed)."""
        if job_id is None:
            return None
        self.get(job_id)  # unknown job -> JobError
        return None if job_id == self.default_id() else job_id

    def candidate_job(self, entry) -> str:
        return entry.job_id or self.default_id()

    def description(self, job_id: str | None):
        from .config import JobDescription

        return JobDescription.model_validate(self.get(job_id).job)

    def questions(self, job_id: str | None):
        from .config import ScreeningQuestions

        return ScreeningQuestions.model_validate({"questions": self.get(job_id).questions})

    # -- write --------------------------------------------------------------
    def save(self, rec: JobRecord) -> JobRecord:
        from .config import JobDescription, ScreeningQuestions

        from pydantic import ValidationError

        try:
            jd = JobDescription.model_validate(rec.job)
            qs = ScreeningQuestions.model_validate({"questions": rec.questions})
        except ValidationError as e:
            errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:6])
            raise JobError(f"the job isn't valid yet: {errs}") from None
        rec = rec.model_copy(update={"job": jd.model_dump(exclude_none=True),
                                     "questions": [q.model_dump() for q in qs.questions], "updated_at": utc_now()})
        self.store.put_record("jobs", jd.job_id, rec.model_dump(mode="json"))
        self._load()[jd.job_id] = rec
        return rec

    def create(self, job: dict, questions: list[dict], *, posted_by: str | None = None,
               deadline: str | None = None) -> JobRecord:
        job = dict(job)
        base = job.get("job_id") or f"{slug(job.get('title') or 'job')}-{date.today():%Y%m%d}"
        jid, n = base, 2
        while jid in self._load():
            jid, n = f"{base}-{n}", n + 1
        job["job_id"] = jid
        return self.save(JobRecord(job=job, questions=questions, posted_by=posted_by, deadline=parse_deadline(deadline)))

    def set_status(self, job_id: str, status: JobStatus) -> JobRecord:
        return self.save(self.get(job_id).model_copy(update={"status": status}))

    def update(self, job_id: str, job: dict, questions: list[dict], **extra) -> JobRecord:
        """extra: deadline=<ISO date | None> to change it (left out: kept)."""
        rec = self.get(job_id)
        upd = {"job": {**job, "job_id": job_id}, "questions": questions}
        if "deadline" in extra:
            upd["deadline"] = parse_deadline(extra["deadline"])
        return self.save(rec.model_copy(update=upd))

    def _import_yaml_job(self) -> dict[str, JobRecord]:
        """First run with jobs: the YAML job becomes the default job."""
        from .config import ConfigError

        try:
            jd, qs = self.config.yaml_job(), self.config.yaml_questions()
        except ConfigError:
            return {}
        rec = JobRecord(job=jd.model_dump(exclude_none=True), questions=[q.model_dump() for q in qs.questions],
                        is_default=True)
        self.store.put_record("jobs", jd.job_id, rec.model_dump(mode="json"))
        return {jd.job_id: rec}

