"""Shared fixtures. FakeLLM stands in for Gemini so the pipeline is tested offline."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

# Tests use the JSON-file backend unless a test opts into MongoDB explicitly,
# and never send real email: messages go to <data>/outbox as .eml files.
os.environ["STORAGE_BACKEND"] = "file"
os.environ["EMAIL_MODE"] = "outbox"

from screening.config import load_config
from screening.context import RunContext
from screening.llm.base import LLMClient, LLMResponseError
from screening.paths import PROJECT_ROOT
from screening.schemas import (
    AgentTurnLLM,
    InterviewEvaluationLLM,
    ResumeExtractionLLM,
    ShortlistAssessmentLLM,
    TranscriptParseLLM,
)

SAMPLES = PROJECT_ROOT / "samples"


def _profile(name, phone, skills, years, title):
    return {
        "full_name": name, "email": f"{name.split()[0].lower()}@example.com", "phone": phone,
        "location": "Hyderabad, India", "linkedin_url": None, "headline": title, "summary": f"{title}.",
        "total_experience_years": years, "current_title": title, "current_company": "Acme",
        "skills": skills,
        "work_experience": [{"title": title, "company": "Acme", "location": None, "start_date": "2022-04",
                             "end_date": None, "is_current": True, "highlights": ["Did things."]}],
        "education": [{"degree": "B.Tech", "field_of_study": "CS", "institution": "JNTU", "graduation_year": 2019}],
        "certifications": [], "languages": ["English"],
    }


PROFILES = {
    "Aarav Sharma": _profile("Aarav Sharma", "+91 90000 00001",
                             ["Python", "FastAPI", "SQL", "Git", "Docker", "AWS"], 6.0, "Senior Software Engineer"),
    "Priya Nair": _profile("Priya Nair", "+91 90000 00002", ["Python", "SQL", "Git"], 2.5, "Data Analyst"),
    "Rohan Mehta": _profile("Rohan Mehta", None, ["React", "TypeScript", "Git"], 4.0, "Frontend Developer"),
}


def _crit(score):
    return {"score": score, "evidence": "evidence"}


ASSESSMENTS = {  # keyed by the headline, since names are blinded in Stage 2 prompts
    "Senior Software Engineer": {
        "must_have_skills": _crit(95), "experience": _crit(90), "nice_to_have_skills": _crit(75),
        "role_relevance": _crit(90),
        "matched_must_have_skills": ["python", "REST API development", "SQL", "Git"],
        "missing_must_have_skills": [], "matched_nice_to_have_skills": ["FastAPI", "Docker", "AWS", "Kubernetes"],
        "rationale": "Strong."},
    "Data Analyst": {
        "must_have_skills": _crit(60), "experience": _crit(40), "nice_to_have_skills": _crit(20),
        "role_relevance": _crit(40),
        "matched_must_have_skills": ["Python", "SQL", "Git"], "missing_must_have_skills": ["REST API development"],
        "matched_nice_to_have_skills": [], "rationale": "Partial."},
    "Frontend Developer": {
        "must_have_skills": _crit(25), "experience": _crit(30), "nice_to_have_skills": _crit(0),
        "role_relevance": _crit(20),
        "matched_must_have_skills": ["Git"], "missing_must_have_skills": ["Python", "SQL"],
        "matched_nice_to_have_skills": [], "rationale": "Weak."},
}

TRANSCRIPT_PARSE = {
    "interested_in_role": True, "current_location": "Hyderabad", "willing_to_relocate": True,
    "notice_period_days": 60, "current_ctc": "22 LPA", "expected_ctc": "30 LPA",
    "available_for_interview": "Weekdays after 6pm",
    "answers": [
        {"question_id": "interest", "question": "x", "answered": True, "answer_summary": "Yes"},
        {"question_id": "notice_period", "question": "x", "answered": True, "answer_summary": "2 months"},
        {"question_id": "made_up", "question": "x", "answered": True, "answer_summary": "?"},
    ],
    "candidate_questions": ["AWS or GCP?"], "red_flags": [], "sentiment": "positive",
    "overall_summary": "Interested.",
}


INVENTED_QUOTE = "I single-handedly migrated forty microservices to Rust"


def interview_evaluation(prompt: str, scores=(80, 70, 75, 90)) -> dict:
    """Stage 4 answer quoting the candidate's real lines (from the prompt's transcript),
    plus one invented quote that Stage 4 must reject."""
    said = [ln.split(":", 1)[1].strip() for ln in prompt.splitlines() if ln.startswith("Candidate:")] or ["-"]
    pick = lambda i: [said[min(i, len(said) - 1)]]  # noqa: E731
    return {
        "role_knowledge": {"score": scores[0], "evidence_quotes": pick(1), "rationale": "Specific."},
        "problem_solving": {"score": scores[1], "evidence_quotes": pick(2), "rationale": "Clear."},
        "communication": {"score": scores[2], "evidence_quotes": pick(3) + [INVENTED_QUOTE], "rationale": "Ok."},
        "motivation": {"score": scores[3], "evidence_quotes": pick(0), "rationale": "Keen."},
        "concerns": [], "summary": "Solid interview.",
    }


def turn(intent, resolved, say, note=None):
    return {"candidate_intent": intent, "current_step_resolved": resolved, "reschedule_note": note, "say": say}


class FakeLLM(LLMClient):
    provider = "fake"
    model = "fake-1"

    def __init__(self, fail_on: set[str] = frozenset(), agent_script: list | None = None):
        self.calls: list[tuple[str, str]] = []
        self.fail_on = fail_on  # substrings of the prompt that trigger an invalid response
        # Queue of AgentTurnLLM dicts (or Exception instances to raise) for the live agent.
        self.agent_script = list(agent_script or [])

    def generate_json(self, *, system, prompt, schema, temperature=None):
        self.calls.append((schema.__name__, prompt))
        if any(s in prompt for s in self.fail_on):
            raise LLMResponseError("response is not valid JSON (simulated)")
        if schema is ResumeExtractionLLM:
            name = next(n for n in PROFILES if n in prompt)
            return schema.model_validate(PROFILES[name])
        if schema is ShortlistAssessmentLLM:
            for n in PROFILES:
                assert n not in prompt, "candidate name must be blinded in the Stage 2 prompt"
            key = next(k for k in ASSESSMENTS if f'"headline": "{k}"' in prompt)
            return schema.model_validate(ASSESSMENTS[key])
        if schema is TranscriptParseLLM:
            return schema.model_validate(TRANSCRIPT_PARSE)
        if schema is InterviewEvaluationLLM:
            return schema.model_validate(interview_evaluation(prompt))
        if schema is AgentTurnLLM:
            item = self.agent_script.pop(0) if self.agent_script else turn("answer", True, "Thanks. Next question?")
            if isinstance(item, Exception):
                raise item
            return schema.model_validate(item)
        raise AssertionError(schema)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    resumes = tmp_path / "data" / "input" / "resumes"
    resumes.mkdir(parents=True)
    for f in (SAMPLES / "resumes").glob("*.docx"):
        shutil.copy(f, resumes / f.name)
    return tmp_path / "data"


@pytest.fixture
def ctx(data_dir: Path) -> RunContext:
    c = RunContext.create(load_config(data_dir=data_dir))
    # Most tests drive Stage 3 directly; the approval gates have their own tests (test_approvals.py).
    c.config.settings.approvals.require_shortlist_approval = False
    return c
