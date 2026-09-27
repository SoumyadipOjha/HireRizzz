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

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"

StageName = Literal["stage1_extraction", "stage2_shortlisting", "stage3_calling", "stage4_evaluation"]
STAGES: tuple[StageName, ...] = ("stage1_extraction", "stage2_shortlisting", "stage3_calling", "stage4_evaluation")
StageStatus = Literal["pending", "success", "failed", "skipped", "awaiting"]
OverallStatus = Literal["active", "rejected", "failed", "fraud", "cooling", "awaiting", "completed", "evaluated",
                        "selected", "not_selected"]
Decision = Literal["shortlisted", "rejected"]
COMPETENCIES = ("role_knowledge", "problem_solving", "communication", "motivation")


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


ReviewGate = Literal["shortlist", "final"]
REVIEW_GATES: tuple[ReviewGate, ...] = ("shortlist", "final")


class Review(_Model):
    """A person's decision at an approval gate (approvals.py)."""
    decision: Decision
    ai_decision: Decision | None = None   # what the AI suggested at the time
    by: str                               # who approved (free text: there is no login yet)
    at: str
    note: str | None = None

    @property
    def overridden(self) -> bool:
        return self.ai_decision is not None and self.ai_decision != self.decision


class Notification(_Model):
    """A result email to the candidate (approvals.py)."""
    kind: str                             # resume_rejected | selected | not_selected | clarification
    status: Literal["sent", "outbox", "failed", "skipped"]
    to: str | None = None
    at: str
    error: str | None = None


class CandidateEntry(_Model):
    candidate_id: str
    source_file: str
    source_sha256: str | None
    ingested_at: str
    updated_at: str
    job_id: str | None = None              # the job applied to (None: from before jobs = the default job)
    display_name: str | None = None
    current_stage: StageName = "stage1_extraction"
    overall_status: OverallStatus = "active"
    stages: dict[StageName, StageState] = Field(default_factory=lambda: {s: StageState() for s in STAGES})
    reviews: dict[ReviewGate, Review] = Field(default_factory=dict)
    # keyed by what it follows: the "shortlist" / "final" gate, or "credibility" (the clarification request)
    notifications: dict[Literal["shortlist", "final", "credibility", "screening"], Notification] = Field(default_factory=dict)
    credibility: "CredibilitySummary | None" = None  # resume checks / LinkedIn cross-check (credibility.py)
    fraud_blocked: bool = False            # stopped by the credibility checks (no further stages)
    fraud_cleared: "FraudClearance | None" = None   # a recruiter looked and let the candidate continue
    cooling: "CoolingPeriod | None" = None           # same email screened recently (cooldown.py)

    @model_validator(mode="after")
    def _all_stages(self) -> "CandidateEntry":
        # Entries written before a stage existed get it as `pending`.
        for s in STAGES:
            self.stages.setdefault(s, StageState())
        self.stages = {s: self.stages[s] for s in STAGES}
        return self


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


class ExchangeTurn(_Model):
    speaker: Literal["agent", "candidate"]
    text: str
    at: str | None = None


class CallAnswer(_Model):
    """One screening question and exactly what the candidate said about it (built by code, not the LLM)."""
    question_id: str
    question: str
    kind: Literal["role", "logistics"] = "logistics"
    answered: bool                        # the candidate said something while this question was open
    answer_text: str | None               # the candidate's own words for this question, joined in order
    exchange: list[ExchangeTurn]          # the agent's lines (incl. follow-ups) and the candidate's replies
    ai_summary: str | None = None         # the transcript parser's summary of the answer
    mapping: Literal["exact", "inferred"] = "exact"   # inferred = older call, matched from the agent's wording


class Stage3Record(Envelope):
    stage: Literal["stage3_calling"] = "stage3_calling"
    job_id: str
    call: CallInfo
    transcript_path: str
    screening: TranscriptParseLLM
    answers_verbatim: list[CallAnswer] = []


# ---------------------------------------------------------------------------
# Stage 4 — interview evaluation (LLM scores competencies, code decides)
# ---------------------------------------------------------------------------

class CompetencyAssessmentLLM(_Model):
    score: int = Field(ge=0, le=100, description="0-100, per the rubric")
    evidence_quotes: list[str] = Field(
        description="1-3 short quotes copied WORD FOR WORD from the candidate's lines in the transcript; "
                    "empty if the candidate said nothing relevant")
    rationale: str = Field(description="1-2 sentences explaining the score")


class InterviewEvaluationLLM(_Model):
    role_knowledge: CompetencyAssessmentLLM
    problem_solving: CompetencyAssessmentLLM
    communication: CompetencyAssessmentLLM
    motivation: CompetencyAssessmentLLM
    concerns: list[str] = Field(description="Concrete concerns for the hiring team (e.g. vague answers, "
                                            "inconsistency with the resume, notice period); empty if none")
    summary: str = Field(description="2-3 sentence overall assessment of the interview")


class CompetencyResult(_Model):
    competency: Literal["role_knowledge", "problem_solving", "communication", "motivation"]
    score: int
    weight: float
    weighted_score: float
    evidence_quotes: list[str]            # only quotes found verbatim in the candidate's own words
    unverified_quotes: list[str] = []     # quotes the LLM gave that are NOT in the transcript (dropped)
    rationale: str


class Stage4Record(Envelope):
    stage: Literal["stage4_evaluation"] = "stage4_evaluation"
    job_id: str
    competencies: list[CompetencyResult]
    interview_score: float                # Σ competency score × weight
    resume_score: float                   # Stage 2 overall_score
    resume_weight: float
    interview_weight: float
    final_score: float                    # resume × resume_weight + interview × interview_weight
    threshold: float
    suggested_decision: Decision          # the AI's suggestion; people make the final call
    decision_reasons: list[str]
    needs_review: bool                    # low evidence or red flags: a person should look closely
    review_reasons: list[str]
    concerns: list[str]
    summary: str


# ---------------------------------------------------------------------------
# Resume credibility (credibility.py): code checks + LinkedIn PDF cross-check
# ---------------------------------------------------------------------------

FlagLevel = Literal["red", "amber", "green"]


class CredibilityFlag(_Model):
    level: FlagLevel                      # red = contradiction, amber = worth a question, green = confirmed
    check: str                            # e.g. timeline_overlap, experience_inflated, linkedin_dates
    message: str
    resume: str | None = None             # what the resume says
    linkedin: str | None = None           # what the LinkedIn profile says


class CredibilitySummary(_Model):
    red: int = 0
    amber: int = 0
    green: int = 0
    linkedin: bool = False                # a LinkedIn PDF was compared
    updated_at: str | None = None


class FraudClearance(_Model):
    by: str
    at: str
    note: str | None = None
    red_at_clearance: int = 0             # how many issues the recruiter saw when clearing


class CoolingPeriod(_Model):
    """This email address finished a screening call recently: no new screening until `until`."""
    since: str                            # when that screening call finished
    until: str
    previous_candidate_id: str
    previous_job_id: str | None = None
    previous_job_title: str | None = None
    cleared: "FraudClearance | None" = None   # a recruiter let this application through anyway


class CredibilityRecord(_Model):
    schema_version: str = SCHEMA_VERSION
    candidate_id: str
    created_at: str = Field(default_factory=utc_now)
    flags: list[CredibilityFlag]
    summary: CredibilitySummary
    linkedin_file: str | None = None      # uploaded file name
    linkedin_profile: ResumeExtractionLLM | None = None  # what Gemini read from the LinkedIn PDF
    llm: LLMInfo | None = None


# ---------------------------------------------------------------------------
# JD writer (jd_writer.py): hiring manager's brief -> draft JD + role questions
# ---------------------------------------------------------------------------

class RoleQuestionLLM(_Model):
    question: str = Field(description="One spoken interview question about real work in this role; "
                                      "answerable in about a minute, no yes/no questions")


class JDDraftLLM(_Model):
    title: str = Field(description="Job title, plain and searchable (no 'rockstar'/'ninja')")
    location: str | None = Field(description="Location and work mode (e.g. 'Hyderabad, India (hybrid)'); null if not given")
    min_experience_years: float | None = Field(description="Minimum years of relevant experience; null if not given")
    max_experience_years: float | None = Field(description="Maximum years, only if the brief implies a range; else null")
    must_have_skills: list[str] = Field(description="3-7 skills the role truly cannot do without")
    nice_to_have_skills: list[str] = Field(description="0-8 skills that help but are not required")
    description: str = Field(description="The job post: 1 short paragraph about the role, then a 'Responsibilities:' "
                                         "list of 4-6 lines starting with '- '. Plain text, no markdown headings")
    role_questions: list[RoleQuestionLLM] = Field(description="Exactly 2: one about a recent project using the "
                                                              "must-have skills, one about solving a real problem")
    language_notes: list[str] = Field(description="Wording in the brief that could discourage qualified applicants "
                                                  "(gendered, age-coded, unnecessary requirements) and how the draft "
                                                  "handled it; empty if none")


# CandidateEntry refers to CredibilitySummary, defined further down.
CandidateEntry.model_rebuild()
CandidatesIndexDoc.model_rebuild()

EXPORTED_SCHEMAS: dict[str, type[BaseModel]] = {
    "candidates_index": CandidatesIndexDoc,
    "stage1_extracted": Stage1Record,
    "stage2_shortlist": Stage2Record,
    "stage3_calls": Stage3Record,
    "stage4_evaluation": Stage4Record,
    "credibility": CredibilityRecord,
}
