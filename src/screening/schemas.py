"""Pydantic models for every JSON document in the pipeline (spec §3, §4).

Two kinds of models:
* ``*LLM`` models — exactly what the LLM is asked to return (used as the
  response schema). They never contain candidate_id, weights or decisions.
* Record models — what is written to disk: the LLM output wrapped in an
  envelope stamped by code (candidate_id, run_id, timestamps, model info).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"

StageName = Literal["stage1_extraction", "stage2_shortlisting", "stage3_calling"]
STAGES: tuple[StageName, ...] = ("stage1_extraction", "stage2_shortlisting", "stage3_calling")
StageStatus = Literal["pending", "success", "failed", "skipped", "awaiting"]
OverallStatus = Literal["active", "rejected", "failed", "awaiting", "completed"]
Decision = Literal["shortlisted", "rejected"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# candidates_index.json (spec §3)
# ---------------------------------------------------------------------------

class StageState(_Model):
    status: StageStatus = "pending"
    output_path: str | None = None
    updated_at: str | None = None
    run_id: str | None = None
    error: str | None = None          # set when status == failed: "<Type>: <message>"
    note: str | None = None           # reason when status == skipped / awaiting
    decision: Decision | None = None  # stage2 only
    score: float | None = None        # stage2 only


class CandidateEntry(_Model):
    candidate_id: str
    source_file: str
    source_sha256: str | None
    ingested_at: str
    updated_at: str
    display_name: str | None = None
    current_stage: StageName = "stage1_extraction"
    overall_status: OverallStatus = "active"
    stages: dict[StageName, StageState] = Field(default_factory=lambda: {s: StageState() for s in STAGES})


class CandidatesIndexDoc(_Model):
    schema_version: str = SCHEMA_VERSION
    updated_at: str = Field(default_factory=utc_now)
    candidates: dict[str, CandidateEntry] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Shared envelope (spec §4)
# ---------------------------------------------------------------------------

class LLMInfo(_Model):
    provider: str
    model: str
    prompt_file: str


class Envelope(_Model):
    schema_version: str = SCHEMA_VERSION
    candidate_id: str
    stage: StageName
    run_id: str
    created_at: str = Field(default_factory=utc_now)
    llm: LLMInfo | None


# ---------------------------------------------------------------------------
# Stage 1 — resume extraction (spec §4.1)
# ---------------------------------------------------------------------------

class WorkExperience(_Model):
    title: str | None = Field(description="Job title exactly as written")
    company: str | None = Field(description="Employer name")
    location: str | None = Field(description="Location of the role, if stated")
    start_date: str | None = Field(description="YYYY-MM or YYYY; null if not stated")
    end_date: str | None = Field(description="YYYY-MM or YYYY; null if current or not stated")
    is_current: bool = Field(description="true only if the resume says present/current/till date")
    highlights: list[str] = Field(description="Key responsibilities/achievements, concise, max 6")


class Education(_Model):
    degree: str | None = Field(description="e.g. B.Tech, MSc, MBA")
    field_of_study: str | None
    institution: str | None
    graduation_year: int | None = Field(description="4-digit year or null")


class ResumeExtractionLLM(_Model):
    full_name: str | None = Field(description="Candidate's full name")
    email: str | None
    phone: str | None = Field(description="Phone exactly as written, including country code if present")
    location: str | None = Field(description="Current city/country")
    linkedin_url: str | None
    headline: str | None = Field(description="One-line professional headline, from the resume if present")
    summary: str | None = Field(description="2-3 sentence neutral summary of the profile")
    total_experience_years: float | None = Field(description="Total professional experience in years (stated or computed from roles); null if unknowable")
    current_title: str | None
    current_company: str | None
    skills: list[str] = Field(description="Distinct technical and professional skills, as named in the resume")
    work_experience: list[WorkExperience] = Field(description="Most recent first")
    education: list[Education]
    certifications: list[str]
    languages: list[str] = Field(description="Spoken languages, if listed")


class Stage1Record(Envelope):
    stage: Literal["stage1_extraction"] = "stage1_extraction"
    source_file: str
    source_sha256: str
    resume_char_count: int
    truncated: bool = False
    extraction: ResumeExtractionLLM


# ---------------------------------------------------------------------------
# Stage 2 — shortlisting (spec §4.2)
# ---------------------------------------------------------------------------

class CriterionAssessmentLLM(_Model):
    score: int = Field(ge=0, le=100, description="0-100, per the rubric")
    evidence: str = Field(description="1-2 sentences citing concrete resume facts")


class ShortlistAssessmentLLM(_Model):
    must_have_skills: CriterionAssessmentLLM
    experience: CriterionAssessmentLLM
    nice_to_have_skills: CriterionAssessmentLLM
    role_relevance: CriterionAssessmentLLM
    matched_must_have_skills: list[str] = Field(description="Must-have skills from the JD evidenced in the resume, using the JD's wording")
    missing_must_have_skills: list[str] = Field(description="Must-have skills from the JD NOT evidenced, using the JD's wording")
    matched_nice_to_have_skills: list[str] = Field(description="Nice-to-have skills from the JD evidenced, using the JD's wording")
    rationale: str = Field(description="3-5 sentence overall assessment")


class CriterionResult(_Model):
    criterion: Literal["must_have_skills", "experience", "nice_to_have_skills", "role_relevance"]
    score: int
    weight: float
    weighted_score: float
    evidence: str


class Stage2Record(Envelope):
    stage: Literal["stage2_shortlisting"] = "stage2_shortlisting"
    job_id: str
    criteria: list[CriterionResult]
    matched_must_have_skills: list[str]
    missing_must_have_skills: list[str]
    matched_nice_to_have_skills: list[str]
    overall_score: float
    threshold: float
    require_all_must_have: bool
    decision: Decision
    decision_reasons: list[str]
    rationale: str


# ---------------------------------------------------------------------------
# Stage 3 — calling agent (spec §4.3)
# ---------------------------------------------------------------------------

class ScreeningAnswerLLM(_Model):
    question_id: str = Field(description="The id of the question from the provided list")
    question: str
    answered: bool = Field(description="true only if the candidate actually answered it")
    answer_summary: str | None = Field(description="What the candidate said, concise; null if not answered")


class TranscriptParseLLM(_Model):
    interested_in_role: bool | None
    current_location: str | None
    willing_to_relocate: bool | None
    notice_period_days: int | None = Field(description="Notice period converted to days (1 month = 30); 0 if immediately available; null if not stated")
    current_ctc: str | None = Field(description="Current compensation as stated, with units")
    expected_ctc: str | None = Field(description="Expected compensation as stated, with units")
    available_for_interview: str | None = Field(description="Interview availability as stated")
    answers: list[ScreeningAnswerLLM] = Field(description="One entry per question in the provided list, same order")
    candidate_questions: list[str] = Field(description="Questions the candidate asked the agent")
    red_flags: list[str] = Field(description="Concrete concerns (inconsistency with resume, hostility, etc.); empty if none")
    sentiment: Literal["positive", "neutral", "negative"]
    overall_summary: str = Field(description="3-4 sentence summary of the call")


CallChannel = Literal["browser_voice", "browser_text", "terminal_simulation", "local_transcript_file"]
CallOutcome = Literal["completed", "rescheduled", "opted_out", "abandoned", "failed"]


class CallInfo(_Model):
    agent: str = "in_house_screening_agent"
    channel: CallChannel
    session_id: str | None
    status: CallOutcome
    started_at: str | None = None
    ended_at: str | None = None
    duration_seconds: float | None = None
    agent_turns: int = 0
    candidate_turns: int = 0
    questions_asked: int = 0
    reschedule_note: str | None = None   # when the candidate asked to be called another time
    agent_llm: str | None = None         # model that ran the live conversation


# ---------------------------------------------------------------------------
# Stage 3 — live agent (one LLM call per candidate turn)
# ---------------------------------------------------------------------------

class AgentTurnLLM(_Model):
    candidate_intent: Literal["answer", "consent_yes", "reschedule", "opt_out", "question", "unclear", "other"] = Field(
        description="What the candidate's latest message is doing")
    current_step_resolved: bool = Field(
        description="true if the CURRENT step is done: consent given, or the current question answered "
                    "(or clearly declined) well enough to move on")
    reschedule_note: str | None = Field(description="If they want another time: when, in their words; else null")
    say: str = Field(description="Exactly what the agent says next, spoken aloud: 1-3 short sentences, no lists, no markdown")


class Stage3Record(Envelope):
    stage: Literal["stage3_calling"] = "stage3_calling"
    job_id: str
    call: CallInfo
    transcript_path: str
    screening: TranscriptParseLLM


EXPORTED_SCHEMAS: dict[str, type[BaseModel]] = {
    "candidates_index": CandidatesIndexDoc,
    "stage1_extracted": Stage1Record,
    "stage2_shortlist": Stage2Record,
    "stage3_calls": Stage3Record,
}
