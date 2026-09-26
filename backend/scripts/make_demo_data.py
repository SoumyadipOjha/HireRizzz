"""Build a DEMO data directory (demo_runs/demo) so the dashboard has something to show
before a Gemini key is configured.

It runs the REAL pipeline code (ingest, Stages 1-3, transcript parser) but with a
scripted DemoLLM instead of Gemini: its answers are hand-written to match the
synthetic sample resumes. Every record says provider "demo", and the folder gets
a DEMO_DATA.txt marker so the dashboard shows a DEMO banner. Your real data/ is
never touched.

Run:  uv run python scripts/make_demo_data.py
Then: uv run screening --data-dir demo_runs/demo dashboard
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from screening.config import load_config  # noqa: E402
from screening.context import RunContext  # noqa: E402
from screening.dashboard.server import DEMO_MARKER  # noqa: E402
from screening.llm.base import LLMClient  # noqa: E402
from screening.agent.dialogue import ScreeningDialogue  # noqa: E402
from screening.schemas import AgentTurnLLM, ResumeExtractionLLM, ShortlistAssessmentLLM, TranscriptParseLLM  # noqa: E402
from screening.stage0_ingest import ingest  # noqa: E402
from screening.stage1_extract import run_stage1  # noqa: E402
from screening.stage2_shortlist import run_stage2  # noqa: E402
from screening.stage3_call import finalize_dialogue, run_stage3  # noqa: E402

DEMO = ROOT / "demo_runs" / "demo"


def _job(title, company, loc, start, end, highlights):
    return {"title": title, "company": company, "location": loc, "start_date": start, "end_date": end,
            "is_current": end is None, "highlights": highlights}


PROFILES = {
    "Aarav Sharma": {
        "full_name": "Aarav Sharma", "email": "aarav.sharma@example.com", "phone": "+91 90000 00001",
        "location": "Hyderabad, India", "linkedin_url": "linkedin.com/in/aarav-sharma-example",
        "headline": "Senior Python Backend Engineer — 6 years building REST APIs",
        "summary": "Backend engineer with 6 years of experience designing and operating Python services and REST APIs on AWS. Owns features end to end, from schema design to CI/CD and monitoring.",
        "total_experience_years": 6.0, "current_title": "Senior Software Engineer", "current_company": "Finlytics Pvt Ltd",
        "skills": ["Python", "SQL", "Bash", "FastAPI", "Django", "Django REST Framework", "Celery", "PostgreSQL", "Redis",
                   "SQLAlchemy", "Docker", "GitHub Actions (CI/CD)", "AWS (ECS, Lambda, RDS, S3)", "Terraform", "Git",
                   "code review", "pytest", "TDD", "agile/Scrum"],
        "work_experience": [
            _job("Senior Software Engineer", "Finlytics Pvt Ltd", "Hyderabad", "2022-04", None,
                 ["Lead developer of a FastAPI payments API serving 2M requests/day on AWS ECS.",
                  "Redesigned PostgreSQL schema and queries, cutting p95 latency from 480ms to 120ms.",
                  "Introduced GitHub Actions CI/CD with automated pytest suites (85% coverage)."]),
            _job("Software Engineer", "CloudKart Technologies", "Bengaluru", "2019-07", "2022-03",
                 ["Built Django REST Framework services for order management.",
                  "Containerised 12 services with Docker; migrated deployments to AWS.",
                  "Mentored 3 junior engineers; ran weekly code reviews."])],
        "education": [{"degree": "B.Tech", "field_of_study": "Computer Science and Engineering",
                       "institution": "JNTU Hyderabad", "graduation_year": 2019}],
        "certifications": ["AWS Certified Developer – Associate (2023)"], "languages": ["English", "Hindi", "Telugu"],
    },
    "Priya Nair": {
        "full_name": "Priya Nair", "email": "priya.nair@example.com", "phone": "+91 90000 00002",
        "location": "Kochi, Kerala", "linkedin_url": None,
        "headline": "Data Analyst moving into backend development",
        "summary": "Data analyst with 2.5 years of experience automating reporting with Python and SQL. Has built a few internal Flask endpoints and is learning FastAPI and Docker.",
        "total_experience_years": 2.5, "current_title": "Data Analyst", "current_company": "Coastal Retail Analytics",
        "skills": ["Python (pandas, NumPy)", "SQL", "Power BI", "Excel", "Jupyter", "Git", "MySQL", "PostgreSQL (basic)",
                   "FastAPI", "Docker", "Flask"],
        "work_experience": [
            _job("Data Analyst", "Coastal Retail Analytics", "Kochi", "2023-01", None,
                 ["Automated 20+ weekly Excel reports using Python and SQL, saving ~15 hours/week.",
                  "Wrote two small Flask endpoints exposing sales metrics to the BI team.",
                  "Maintained scripts in a shared Git repository."]),
            _job("Analyst Intern", "Coastal Retail Analytics", "Kochi", "2022-06", "2022-12",
                 ["Cleaned and validated sales data; built Power BI dashboards."])],
        "education": [{"degree": "B.Sc.", "field_of_study": "Mathematics", "institution": "University of Kerala",
                       "graduation_year": 2022}],
        "certifications": [], "languages": [],
    },
    "Rohan Mehta": {
        "full_name": "Rohan Mehta", "email": "rohan.mehta@example.com", "phone": "+91 90000 00003",
        "location": "Pune, Maharashtra", "linkedin_url": None,
        "headline": "Frontend Developer — React & TypeScript",
        "summary": "Frontend developer with 4 years of experience building responsive web apps in React and TypeScript, consuming REST APIs daily.",
        "total_experience_years": 4.0, "current_title": "Frontend Developer", "current_company": "PixelCraft Studios",
        "skills": ["React", "TypeScript", "JavaScript", "Redux", "HTML5", "CSS3", "Tailwind", "Jest",
                   "React Testing Library", "Cypress", "Git", "Webpack", "Vite", "Figma"],
        "work_experience": [
            _job("Frontend Developer", "PixelCraft Studios", "Pune", "2021-08", None,
                 ["Built a React/TypeScript design system used across 5 products.",
                  "Integrated REST APIs from backend teams; improved Lighthouse score from 62 to 94."]),
            _job("Junior Web Developer", "WebNest Solutions", "Pune", "2020-07", "2021-07",
                 ["Developed marketing sites with HTML, CSS and vanilla JavaScript."])],
        "education": [{"degree": "B.E.", "field_of_study": "Information Technology",
                       "institution": "Savitribai Phule Pune University", "graduation_year": 2020}],
        "certifications": [], "languages": [],
    },
    "Sneha Iyer": {
        "full_name": "Sneha Iyer", "email": "sneha.iyer@example.com", "phone": "+91 90000 00004",
        "location": "Chennai, Tamil Nadu", "linkedin_url": None,
        "headline": "Python Developer — APIs and data services",
        "summary": "Python developer with 3.5 years of experience building Flask and FastAPI services backed by PostgreSQL, deployed with Docker.",
        "total_experience_years": 3.5, "current_title": "Python Developer", "current_company": "MedTrack Systems",
        "skills": ["Python", "Flask", "FastAPI", "PostgreSQL", "SQL", "Docker", "Git", "REST APIs", "pytest", "Jenkins"],
        "work_experience": [
            _job("Python Developer", "MedTrack Systems", "Chennai", "2022-02", None,
                 ["Built REST APIs in FastAPI for a patient-scheduling product.",
                  "Wrote SQL migrations and query optimisations on PostgreSQL.",
                  "Dockerised services and maintained a Jenkins pipeline."]),
            _job("Associate Developer", "InfoBridge Solutions", "Chennai", "2021-03", "2022-01",
                 ["Maintained Flask internal tools and wrote unit tests with pytest."])],
        "education": [{"degree": "B.E.", "field_of_study": "Electronics and Communication",
                       "institution": "Anna University", "graduation_year": 2020}],
        "certifications": [], "languages": ["English", "Tamil"],
    },
}


def _c(score, evidence):
    return {"score": score, "evidence": evidence}


ASSESSMENTS = {  # keyed by headline, because names are hidden from the Stage 2 prompt
    "Senior Python Backend Engineer — 6 years building REST APIs": {
        "must_have_skills": _c(95, "Python, REST APIs (FastAPI, DRF), SQL (PostgreSQL) and Git are all used in current and previous roles."),
        "experience": _c(92, "6 years of backend experience, within the 3-8 year range, all directly relevant."),
        "nice_to_have_skills": _c(95, "FastAPI, Docker, AWS, PostgreSQL and CI/CD (GitHub Actions) are all evidenced."),
        "role_relevance": _c(94, "Currently leads a production FastAPI service on AWS — essentially this job."),
        "matched_must_have_skills": ["Python", "REST API development", "SQL", "Git"], "missing_must_have_skills": [],
        "matched_nice_to_have_skills": ["FastAPI", "Docker", "AWS", "PostgreSQL", "CI/CD"],
        "rationale": "An unusually close match: every must-have and nice-to-have skill is evidenced with measurable outcomes (latency, coverage, traffic). Experience sits comfortably inside the target range and includes mentoring. Recommend progressing.",
    },
    "Python Developer — APIs and data services": {
        "must_have_skills": _c(85, "Python, REST APIs (FastAPI/Flask), SQL on PostgreSQL and Git are all used professionally."),
        "experience": _c(72, "3.5 years, at the lower end of the 3-8 year range, all in Python service development."),
        "nice_to_have_skills": _c(60, "FastAPI, Docker and PostgreSQL evidenced; Jenkins counts as CI/CD; no AWS."),
        "role_relevance": _c(82, "Builds and deploys Python REST services today; smaller scale than this role."),
        "matched_must_have_skills": ["Python", "REST API development", "SQL", "Git"], "missing_must_have_skills": [],
        "matched_nice_to_have_skills": ["FastAPI", "Docker", "PostgreSQL", "CI/CD"],
        "rationale": "Solid mid-level backend profile covering all must-haves. Experience meets the minimum but not by much, and there is no cloud (AWS) exposure. Worth a screening call.",
    },
    "Data Analyst moving into backend development": {
        "must_have_skills": _c(55, "Python, SQL and Git are used daily; REST API work is limited to two small Flask endpoints."),
        "experience": _c(35, "2.5 years including an internship, below the 3-year minimum, and mostly analytics rather than backend."),
        "nice_to_have_skills": _c(15, "PostgreSQL only at a basic level; FastAPI and Docker are listed as 'learning'."),
        "role_relevance": _c(30, "Adjacent analytics role; backend work is incidental."),
        "matched_must_have_skills": ["Python", "SQL", "Git"], "missing_must_have_skills": ["REST API development"],
        "matched_nice_to_have_skills": ["PostgreSQL"],
        "rationale": "Promising Python and SQL skills, but the profile is analytics-focused with minimal API development and is below the experience minimum. Not a fit for this role yet.",
    },
    "Frontend Developer — React & TypeScript": {
        "must_have_skills": _c(25, "Git is evidenced and REST APIs are consumed rather than built; no Python or SQL."),
        "experience": _c(30, "4 years of professional experience, but in frontend development rather than backend."),
        "nice_to_have_skills": _c(0, "None of FastAPI, Docker, AWS, PostgreSQL or CI/CD is evidenced."),
        "role_relevance": _c(15, "Frontend UI engineering; unrelated to backend service ownership."),
        "matched_must_have_skills": ["Git"], "missing_must_have_skills": ["Python", "REST API development", "SQL"],
        "matched_nice_to_have_skills": [],
        "rationale": "A strong frontend profile, but it does not cover the core backend requirements (Python, API development, SQL). Not a match.",
    },
}

TRANSCRIPT = {
    "interested_in_role": True, "current_location": "Hyderabad (Gachibowli)", "willing_to_relocate": True,
    "notice_period_days": 60, "current_ctc": "~22 LPA", "expected_ctc": "~30 LPA",
    "available_for_interview": "Any weekday after 6 pm, or Saturday mornings",
    "answers": [
        {"question_id": "interest", "question": "", "answered": True, "answer_summary": "Actively looking; says the role is a good fit given 3 years of FastAPI work."},
        {"question_id": "current_location", "question": "", "answered": True, "answer_summary": "Hyderabad, Gachibowli."},
        {"question_id": "relocation", "question": "", "answered": True, "answer_summary": "Hybrid is fine; 3 days a week in office works."},
        {"question_id": "notice_period", "question": "", "answered": True, "answer_summary": "Two months, possibly negotiable to about 45 days."},
        {"question_id": "current_ctc", "question": "", "answered": True, "answer_summary": "Around 22 lakhs per annum."},
        {"question_id": "expected_ctc", "question": "", "answered": True, "answer_summary": "Around 30 LPA."},
        {"question_id": "interview_availability", "question": "", "answered": True, "answer_summary": "Weekdays after 6 pm or Saturday mornings."},
    ],
    "candidate_questions": ["Is the team using AWS or GCP?"], "red_flags": [], "sentiment": "positive",
    "overall_summary": "Aarav is actively looking and enthusiastic about the role. Aarav is based in Hyderabad and comfortable with hybrid work. Notice period is two months (possibly 45 days); current CTC ~22 LPA, expecting ~30 LPA. Available for interviews on weekday evenings or Saturday mornings.",
}


def _t(intent, say, resolved=True, note=None):
    return {"candidate_intent": intent, "current_step_resolved": resolved, "reschedule_note": note, "say": say}


# (candidate line, scripted agent decision) — run through the REAL dialogue engine.
AARAV_CALL = [
    ("Sure, go ahead.", _t("consent_yes", "Great, thank you. To start, are you currently open to new opportunities, and does this role interest you?")),
    ("Yes, I'm actively looking, and the role sounds like a good fit. I've been doing FastAPI work for the last three years.",
     _t("answer", "That's great to hear. Where are you currently based?")),
    ("Hyderabad, Gachibowli.", _t("answer", "Thanks. Would you be comfortable working hybrid from our Hyderabad office?")),
    ("Hybrid is fine, three days a week in office works for me.", _t("answer", "Perfect. What is your current notice period?")),
    ("It's two months.", _t("answer", "Got it. Is there any flexibility on that?", resolved=False)),
    ("It might be negotiable down to about forty five days.", _t("answer", "Thanks, that helps. What is your current compensation?")),
    ("Around twenty two lakhs per annum.", _t("answer", "Thank you. And what are your compensation expectations for this role?")),
    ("I'm looking for something around thirty LPA.", _t("answer", "Noted. Lastly, when would you be available for a technical interview?")),
    ("Any weekday after 6 pm, or Saturday mornings. Quick question, is the team using AWS or GCP?",
     _t("answer", "Thanks, Aarav. I don't have details on the cloud setup, but the recruiting team will follow up on that. That's everything I needed. The team will be in touch soon. Goodbye!")),
]


class DemoLLM(LLMClient):
    provider = "demo"
    model = "scripted-demo-llm (not Gemini)"

    def __init__(self):
        self.agent_script = [a for _, a in AARAV_CALL]

    def generate_json(self, *, system, prompt, schema, temperature=None):
        if schema is ResumeExtractionLLM:
            return schema.model_validate(next(p for n, p in PROFILES.items() if n in prompt))
        if schema is ShortlistAssessmentLLM:
            return schema.model_validate(next(a for k, a in ASSESSMENTS.items() if f'"headline": "{k}"' in prompt))
        if schema is TranscriptParseLLM:
            return schema.model_validate(TRANSCRIPT)
        if schema is AgentTurnLLM:
            return schema.model_validate(self.agent_script.pop(0))
        raise AssertionError(schema)


def _make_sneha(path: Path) -> None:
    spec = importlib.util.spec_from_file_location("msr", ROOT / "scripts" / "make_sample_resumes.py")
    msr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(msr)
    d = msr._doc("Sneha Iyer", "sneha.iyer@example.com | +91 90000 00004 | Chennai, Tamil Nadu",
                 "Python Developer — APIs and data services")
    d.add_heading("Summary", level=1)
    d.add_paragraph("Python developer with 3.5 years of experience building Flask and FastAPI services backed by "
                    "PostgreSQL, deployed with Docker.")
    msr._skills_table(d, [("Languages", "Python, SQL"), ("Frameworks", "Flask, FastAPI"),
                          ("Data", "PostgreSQL"), ("Tools", "Docker, Git, Jenkins, pytest")])
    d.add_heading("Experience", level=1)
    msr._job(d, "Python Developer — MedTrack Systems, Chennai", "Feb 2022 – Present",
             ["Built REST APIs in FastAPI for a patient-scheduling product.",
              "Wrote SQL migrations and query optimisations on PostgreSQL.",
              "Dockerised services and maintained a Jenkins pipeline."])
    msr._job(d, "Associate Developer — InfoBridge Solutions, Chennai", "Mar 2021 – Jan 2022",
             ["Maintained Flask internal tools and wrote unit tests with pytest."])
    d.add_heading("Education", level=1)
    d.add_paragraph("B.E., Electronics and Communication — Anna University, 2020")
    d.save(path)


def main() -> None:
    if DEMO.exists():
        shutil.rmtree(DEMO)
    resumes = DEMO / "input" / "resumes"
    resumes.mkdir(parents=True)
    for f in (ROOT / "samples" / "resumes").glob("*.docx"):
        shutil.copy(f, resumes / f.name)
    _make_sneha(resumes / "sneha_iyer.docx")
    (resumes / "corrupted_upload.docx").write_bytes(b"this upload was truncated and is not a real docx")
    (DEMO / DEMO_MARKER).write_text(
        "Synthetic demo data produced by scripts/make_demo_data.py with a scripted fake LLM (not Gemini).\n",
        encoding="utf-8")

    ctx = RunContext.create(load_config(data_dir=DEMO, storage="file"))
    llm = DemoLLM()
    ingest(ctx)
    run_stage1(ctx, llm)
    run_stage2(ctx, llm)
    run_stage3(ctx)  # interview links for Aarav and Sneha

    # Aarav's call goes through the real ScreeningDialogue state machine (text channel, scripted LLM).
    aarav = next(e for e in ctx.index.all() if e.display_name == "Aarav Sharma")
    d = ScreeningDialogue(config=ctx.config, llm=llm, candidate_id=aarav.candidate_id, candidate_name="Aarav Sharma",
                          channel="browser_text", logger=ctx.logger)
    d.start()
    for line, _ in AARAV_CALL:
        d.reply(line)
    assert d.ended and d.state.outcome == "completed", d.state
    finalize_dialogue(ctx, llm, d)

    sneha = next(e for e in ctx.index.all() if e.display_name == "Sneha Iyer")
    url = ctx.index.get(sneha.candidate_id).stages["stage3_calling"].note.rsplit(": ", 1)[-1]
    print(f"\nDemo data ready in {DEMO}")
    print("Start the server:       uv run screening --data-dir demo_runs/demo serve")
    print(f"Sneha's interview link: {url}   (needs GEMINI_API_KEY to actually talk)")


if __name__ == "__main__":
    main()
