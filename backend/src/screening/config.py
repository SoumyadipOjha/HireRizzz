"""Load and validate configuration. Any problem here aborts the run before
candidates are touched (spec §6, rule 6)."""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .paths import CONFIG_DIR, DEFAULT_DATA_DIR, PROJECT_ROOT, DataPaths, build_data_paths
from .storage import FileStore, Store

CRITERIA = ("must_have_skills", "experience", "nice_to_have_skills", "role_relevance")


class ConfigError(Exception):
    """Invalid or missing configuration."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathsConfig(_Strict):
    input_resumes: str
    candidates_index: str
    stage1_output: str
    stage2_output: str
    stage3_output: str
    stage3_transcripts: str
    stage3_sessions: str
    stage3_invites: str
    stage4_output: str = "stage4_evaluation"
    credibility_output: str = "credibility"
    logs: str


class FilesConfig(_Strict):
    job_description: str
    screening_questions: str


class Thresholds(_Strict):
    min_resume_chars: int = Field(ge=0)
    max_resume_chars: int = Field(gt=0)
    shortlist_score: float = Field(ge=0, le=100)
    require_all_must_have: bool = False

    @model_validator(mode="after")
    def _min_below_max(self) -> "Thresholds":
        if self.min_resume_chars >= self.max_resume_chars:
            raise ValueError("min_resume_chars must be < max_resume_chars")
        return self


class Scoring(_Strict):
    weights: dict[str, float]

    @field_validator("weights")
    @classmethod
    def _check_weights(cls, w: dict[str, float]) -> dict[str, float]:
        if set(w) != set(CRITERIA):
            raise ValueError(f"weights must have exactly these keys: {', '.join(CRITERIA)} (got {sorted(w)})")
        if any(v < 0 for v in w.values()):
            raise ValueError("weights must be non-negative")
        if not math.isclose(sum(w.values()), 1.0, abs_tol=1e-6):
            raise ValueError(f"weights must sum to 1.0 (got {sum(w.values()):.4f})")
        return w


class EvaluationConfig(_Strict):
    """Stage 4: how the interview is scored and combined with the resume score."""
    resume_weight: float = Field(default=0.4, ge=0, le=1)
    interview_weight: float = Field(default=0.6, ge=0, le=1)
    final_threshold: float = Field(default=65, ge=0, le=100)   # final_score >= this => suggest shortlisted
    competency_weights: dict[str, float] = Field(default_factory=lambda: {
        "role_knowledge": 0.35, "problem_solving": 0.25, "communication": 0.20, "motivation": 0.20})
    min_verified_quotes: int = Field(default=1, ge=0)  # fewer verified quotes for a competency => needs_review

    @field_validator("competency_weights")
    @classmethod
    def _check_competencies(cls, w: dict[str, float]) -> dict[str, float]:
        from .schemas import COMPETENCIES

        if set(w) != set(COMPETENCIES):
            raise ValueError(f"competency_weights must have exactly: {', '.join(COMPETENCIES)} (got {sorted(w)})")
        if any(v < 0 for v in w.values()) or not math.isclose(sum(w.values()), 1.0, abs_tol=1e-6):
            raise ValueError(f"competency_weights must be non-negative and sum to 1.0 (got {sum(w.values()):.4f})")
        return w

    @model_validator(mode="after")
    def _weights_sum(self) -> "EvaluationConfig":
        if not math.isclose(self.resume_weight + self.interview_weight, 1.0, abs_tol=1e-6):
            raise ValueError("resume_weight + interview_weight must equal 1.0")
        return self


class CredibilityConfig(_Strict):
    """Resume credibility checks (credibility.py)."""
    block_on_fraud: bool = True           # red issues stop the candidate before any further stage
    min_red_to_block: int = Field(default=1, ge=1)   # how many red issues count as "fraud detected"
    company_check: bool = True            # look each employer up in public records (Wikidata) + website
    # When a candidate is stopped: auto = email them the mismatches and ask for an updated resume,
    # manual = only when a recruiter clicks the button, off = never.
    email_candidate_on_fraud: Literal["auto", "manual", "off"] = "auto"


class ApprovalsConfig(_Strict):
    """Human gates. The AI suggests; people decide (approvals.py)."""
    require_shortlist_approval: bool = True   # invites go out only after a recruiter approves the resume shortlist
    email_resume_rejections: bool = True      # candidates rejected at the resume stage get a polite email


class LLMConfig(_Strict):
    provider: str
    model: str
    temperature: float = Field(default=0.0, ge=0, le=2)
    timeout_seconds: int = Field(default=120, gt=0)
    # Waits (seconds) before retrying a 429 rate-limit / 503 overloaded error; [] = never retry.
    transient_retry_delays: list[float] = Field(default_factory=lambda: [2.0, 5.0], max_length=5)
    # Tried in order when `model` fails (overloaded / free daily quota used up). Same API key.
    fallback_models: list[str] = Field(default_factory=list, max_length=5)
    api_key_env: str = "GEMINI_API_KEY"


class Stage3Config(_Strict):
    public_base_url: str = "http://127.0.0.1:8765"   # used to build interview links
    invite_ttl_days: int = Field(default=7, gt=0)
    max_followups_per_question: int = Field(default=1, ge=0, le=3)
    max_candidate_turns: int = Field(default=40, gt=5)
    max_turn_chars: int = Field(default=2000, gt=50)
    speech_lang: str = "en-IN"                       # browser speech recognition / voice language
    agent_temperature: float = Field(default=0.4, ge=0, le=2)
    verify_email: bool = True                        # candidate enters a code sent to their email before the call
    otp_length: int = Field(default=6, ge=4, le=10)
    otp_expiry_minutes: int = Field(default=10, gt=0)
    otp_max_attempts: int = Field(default=5, gt=0)
    otp_resend_seconds: int = Field(default=30, ge=0)  # minimum gap between two codes

    @field_validator("public_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("public_base_url must start with http:// or https://")
        return v.rstrip("/")


StorageBackend = Literal["file", "mongodb"]
STORAGE_ENV = "STORAGE_BACKEND"  # overrides storage.backend (tests and --storage use it)
EMAIL_MODE_ENV = "EMAIL_MODE"    # overrides email.mode (smtp | outbox)


class StorageConfig(_Strict):
    backend: StorageBackend = "file"
    mongodb_uri_env: str = "MONGODB_URI"
    mongodb_db_env: str = "MONGODB_DB_NAME"
    default_uri: str = "mongodb://localhost:27017"   # used when MONGODB_URI is not set
    default_database: str = "recruiting_screening"


class EmailConfig(_Strict):
    # smtp: send for real (SMTP_* in .env). outbox: write .eml files to <data>/outbox instead (dry run / tests).
    mode: Literal["smtp", "outbox"] = "smtp"
    from_name: str = "Talent Team"
    send_invites: bool = True                 # `screening call` emails each new interview link
    reminder_after_hours: float = Field(default=24, gt=0)
    max_reminders: int = Field(default=1, ge=0)
    auto_reminders: bool = True               # the server sends due reminders in the background
    smtp_host_env: str = "SMTP_HOST"
    smtp_port_env: str = "SMTP_PORT"
    smtp_user_env: str = "SMTP_USER"
    smtp_pass_env: str = "SMTP_PASS"
    smtp_security_env: str = "SMTP_SECURITY"  # starttls (port 587) | ssl (port 465)
    from_address_env: str = "EMAIL_FROM_ADDRESS"


class LoggingConfig(_Strict):
    console_level: str = "INFO"
    file_level: str = "DEBUG"


class Settings(_Strict):
    paths: PathsConfig
    files: FilesConfig
    thresholds: Thresholds
    scoring: Scoring
    llm: LLMConfig
    stage3: Stage3Config = Stage3Config()
    evaluation: EvaluationConfig = EvaluationConfig()
    approvals: ApprovalsConfig = ApprovalsConfig()
    credibility: CredibilityConfig = CredibilityConfig()
    storage: StorageConfig = StorageConfig()
    email: EmailConfig = EmailConfig()
    logging: LoggingConfig = LoggingConfig()


class JobDescription(_Strict):
    job_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    company_name: str = Field(min_length=1)
    location: str | None = None
    min_experience_years: float | None = Field(default=None, ge=0)
    max_experience_years: float | None = Field(default=None, ge=0)
    must_have_skills: list[str] = Field(min_length=1)
    nice_to_have_skills: list[str] = []
    description: str = Field(min_length=1)
    approved_by: str | None = None   # set when a manager approves a JD drafted in the dashboard / CLI
    approved_at: str | None = None


class ScreeningQuestion(_Strict):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    question: str = Field(min_length=1)
    # role = about the job itself (rewritten by the JD writer, scored in Stage 4); logistics = the rest
    kind: Literal["role", "logistics"] = "logistics"


class ScreeningQuestions(_Strict):
    questions: list[ScreeningQuestion] = Field(min_length=1)

    @field_validator("questions")
    @classmethod
    def _unique_ids(cls, qs: list[ScreeningQuestion]) -> list[ScreeningQuestion]:
        ids = [q.id for q in qs]
        if len(ids) != len(set(ids)):
            raise ValueError("question ids must be unique")
        return qs


class AppConfig:
    """Everything a run needs, loaded once."""

    def __init__(self, settings: Settings, data: DataPaths):
        self.settings = settings
        self.data = data
        self._jd: JobDescription | None = None
        self._questions: ScreeningQuestions | None = None
        self._store: Store | None = None

    def reload_files(self) -> None:
        """Forget the cached JD and questions (after they were edited on disk)."""
        self._jd = None
        self._questions = None

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = _make_store(self)
        return self._store

    @property
    def job(self) -> JobDescription:
        if self._jd is None:
            self._jd = _load_model(PROJECT_ROOT / self.settings.files.job_description, JobDescription)
        return self._jd

    @property
    def questions(self) -> ScreeningQuestions:
        if self._questions is None:
            self._questions = _load_model(PROJECT_ROOT / self.settings.files.screening_questions, ScreeningQuestions)
        return self._questions

    def api_key(self) -> str:
        key = os.environ.get(self.settings.llm.api_key_env, "").strip()
        if not key:  # the server is long-lived: pick up a .env created after it started
            load_dotenv(PROJECT_ROOT / ".env", override=False)
            key = os.environ.get(self.settings.llm.api_key_env, "").strip()
        if not key:
            raise ConfigError(
                f"{self.settings.llm.api_key_env} is not set. Copy .env.example to .env and add your key "
                "(free key: https://aistudio.google.com/apikey)."
            )
        return key


def _make_store(config: AppConfig) -> Store:
    sc = config.settings.storage
    if sc.backend == "file":
        return FileStore(config.data)
    from .storage.mongo_store import MongoStore

    uri = os.environ.get(sc.mongodb_uri_env, "").strip() or sc.default_uri
    database = os.environ.get(sc.mongodb_db_env, "").strip() or sc.default_database
    return MongoStore(uri, database)


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return data


def _load_model(path: Path, model: type[BaseModel]):
    try:
        return model.model_validate(_read_yaml(path))
    except ValidationError as e:
        raise ConfigError(f"Invalid config in {path}:\n{e}") from e


def load_config(data_dir: Path | None = None, settings_file: Path | None = None,
                storage: StorageBackend | None = None, email: str | None = None) -> AppConfig:
    """`storage` / `email` (from --storage / --email) win over the STORAGE_BACKEND / EMAIL_MODE env vars,
    which win over settings.yaml."""
    load_dotenv(PROJECT_ROOT / ".env")
    settings = _load_model(settings_file or CONFIG_DIR / "settings.yaml", Settings)
    backend = storage or os.environ.get(STORAGE_ENV, "").strip() or None
    if backend:
        if backend not in ("file", "mongodb"):
            raise ConfigError(f"storage backend must be 'file' or 'mongodb' (got {backend!r})")
        settings = settings.model_copy(
            update={"storage": settings.storage.model_copy(update={"backend": backend})})
    public_url = os.environ.get("PUBLIC_BASE_URL", "").strip()  # where candidates open their interview link
    if public_url:
        try:
            settings = settings.model_copy(
                update={"stage3": Stage3Config.model_validate({**settings.stage3.model_dump(),
                                                               "public_base_url": public_url})})
        except ValidationError as e:
            raise ConfigError(f"PUBLIC_BASE_URL is invalid: {e}") from e
    email_mode = email or os.environ.get(EMAIL_MODE_ENV, "").strip()
    if email_mode:
        if email_mode not in ("smtp", "outbox"):
            raise ConfigError(f"{EMAIL_MODE_ENV} must be 'smtp' or 'outbox' (got {email_mode!r})")
        settings = settings.model_copy(update={"email": settings.email.model_copy(update={"mode": email_mode})})
    data = build_data_paths(data_dir or DEFAULT_DATA_DIR, settings.paths.model_dump())
    return AppConfig(settings, data)
