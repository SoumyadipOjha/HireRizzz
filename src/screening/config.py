"""Load and validate configuration. Any problem here aborts the run before
candidates are touched (spec §6, rule 6)."""

from __future__ import annotations

import math
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .paths import CONFIG_DIR, DEFAULT_DATA_DIR, PROJECT_ROOT, DataPaths, build_data_paths

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


class LLMConfig(_Strict):
    provider: str
    model: str
    temperature: float = Field(default=0.0, ge=0, le=2)
    timeout_seconds: int = Field(default=120, gt=0)
    api_key_env: str = "GEMINI_API_KEY"


class Stage3Config(_Strict):
    public_base_url: str = "http://127.0.0.1:8765"   # used to build interview links
    invite_ttl_days: int = Field(default=7, gt=0)
    max_followups_per_question: int = Field(default=1, ge=0, le=3)
    max_candidate_turns: int = Field(default=40, gt=5)
    max_turn_chars: int = Field(default=2000, gt=50)
    speech_lang: str = "en-IN"                       # browser speech recognition / voice language
    agent_temperature: float = Field(default=0.4, ge=0, le=2)

    @field_validator("public_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("public_base_url must start with http:// or https://")
        return v.rstrip("/")


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


class ScreeningQuestion(_Strict):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    question: str = Field(min_length=1)


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


def load_config(data_dir: Path | None = None, settings_file: Path | None = None) -> AppConfig:
    load_dotenv(PROJECT_ROOT / ".env")
    settings = _load_model(settings_file or CONFIG_DIR / "settings.yaml", Settings)
    data = build_data_paths(data_dir or DEFAULT_DATA_DIR, settings.paths.model_dump())
    return AppConfig(settings, data)
