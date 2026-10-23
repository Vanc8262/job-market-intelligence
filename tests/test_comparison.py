"""Phase 6.3: application comparison against an approved (synthetic) resume."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

import pytest

from comparison import ResumeView, compare_posting, extract_requirements
from comparison.requirements import classify, content_text, requirement_lines
from comparison.runner import ApplicationComparisonRunner, load_results
from dashboard.app import attach_application_fit, create_server
from models.candidate import ReviewApproval, ReviewSection
from models.job_posting import (
    CompanyInfo,
    DescriptionInfo,
    ExperienceInfo,
    JobPosting,
    PositionInfo,
    PostingMetadata,
    SkillInfo,
    SourceInfo,
    SourceType,
)
from processors import JobPostingNormalizer
from profiles import ResumeIngestionService
from profiles.parser import ResumeParser, SkillVocabulary
from storage import SilverStorage
from tests.support.resumes import SAMPLE_RESUME, make_pdf
from utils.config import Config

POSTING_TEXT = """About the Role
We build data products for healthcare clinics and patients.
What You'll Do
Build Airflow pipelines that feed our clinical analytics.
To thrive in this role, you have
2+ years of experience building data pipelines
Strong Python and SQL skills
Experience with Spark or Kafka
Bachelor's degree in Computer Science, Statistics, or a related field
Excellent communication with clinical stakeholders
Nice to have
Experience with Snowflake
Benefits
Medical, dental, and vision insurance for you and your family
"""


def posting(
    text: str = POSTING_TEXT,
    *,
    job_id: str = "job-1",
    seniority: str = "mid",
    min_years: float | None = 2,
    qualifications: list[str] | None = None,
) -> JobPosting:
    normalizer = JobPostingNormalizer()
    raw = JobPosting(
        metadata=PostingMetadata(job_id=job_id),
        source=SourceInfo(name="fixture", source_type=SourceType.ATS, external_id=job_id),
        company=CompanyInfo(name="Example Health", normalized_name="Example Health"),
        position=PositionInfo(title="Data Engineer", seniority=seniority),
        description=DescriptionInfo(clean_text=text, qualifications=qualifications or []),
        experience=ExperienceInfo(min_years=min_years),
        skills=SkillInfo(),
    )
    normalized = normalizer.normalize(raw)
    return normalized.model_copy(
        update={"position": normalized.position.model_copy(update={"seniority": seniority})}
    )


@pytest.fixture(scope="module")
def resume() -> ResumeView:
    config = Config()
    parser = ResumeParser(SkillVocabulary(config.skills, config.resume_profile.skills))
    return ResumeView.from_facts(parser.parse(SAMPLE_RESUME).facts, SAMPLE_RESUME)


def skills_in_text(text: str) -> list[str]:
    return JobPostingNormalizer().skills_in_text(text)


def test_requirement_sections_skip_duties_and_benefits() -> None:
    lines = requirement_lines(posting())

    texts = [text for text, _ in lines]
    assert "2+ years of experience building data pipelines" in texts
    assert "Experience with Snowflake" in texts
    assert not any("Airflow pipelines" in text or "insurance" in text for text in texts)
    required = dict(lines)
    assert required["Strong Python and SQL skills"] is True
    assert required["Experience with Snowflake"] is False  # under "Nice to have"
    assert "insurance" not in content_text(posting())  # benefits never count as domain


def test_lever_qualifications_and_flat_text_fallback() -> None:
    lever = posting(qualifications=["Python and SQL", "Kubernetes is a plus"])
    assert requirement_lines(lever) == [("Python and SQL", True), ("Kubernetes is a plus", False)]

    flat = posting(
        "We are hiring. Candidates need 3+ years of experience with Python. Lunch is free."
    )
    assert [text for text, _ in requirement_lines(flat)] == [
        "Candidates need 3+ years of experience with Python."
    ]


def test_requirement_classification() -> None:
    either = classify("Experience with Spark or Kafka", True, skills_in_text)
    assert either.kind == "skill" and either.skills == ["Spark", "Kafka"] and either.any_of
    both = classify("Strong Python and SQL skills", True, skills_in_text)
    assert both.kind == "skill" and not both.any_of
    degree = classify("B.S. in Computer Science, Statistics, or Mathematics", True, skills_in_text)
    assert degree.kind == "degree" and degree.degree_level == 3  # fields are not skills
    years = classify("5+ years of experience in production systems", True, skills_in_text)
    assert years.kind == "years" and years.min_years == 5
    assert classify("Excellent communication skills", True, skills_in_text).kind == "text"


def test_comparison_scores_and_explains_each_requirement(resume: ResumeView) -> None:
    job = posting()
    frequencies = Counter({"Python": 50, "SQL": 40, "Spark": 5, "Kafka": 4, "Snowflake": 3})

    result = compare_posting(
        job,
        resume,
        Config().comparison,
        skills_in_text=skills_in_text,
        frequencies=frequencies,
        corpus_size=60,
    )

    items = {item["text"]: item for item in result["dimensions"]["requirements"]["items"]}
    python_sql = items["Strong Python and SQL skills"]
    assert python_sql["status"] == "met"
    assert {match["skill"] for match in python_sql["matched"]} == {"Python", "SQL"}
    assert all(match["evidence"]["text"] for match in python_sql["matched"])
    assert items["Experience with Spark or Kafka"]["status"] == "missing"
    assert items["2+ years of experience building data pipelines"]["status"] == "met"
    degree = items["Bachelor's degree in Computer Science, Statistics, or a related field"]
    assert degree["status"] == "met"  # resume: B.S. Data Science and Analytics
    assert items["Experience with Snowflake"]["required"] is False

    keywords = result["dimensions"]["keywords"]
    to_add = [item["skill"] for item in keywords["to_add"]]
    assert "Spark" in to_add and "Kafka" in to_add
    assert keywords["to_add"][0]["in_requirements"] is True
    assert result["dimensions"]["seniority"]["score"] == 1.0
    domain = result["dimensions"]["domain"]
    assert "healthcare" in domain["posting_domains"] and domain["score"] == 0.0
    assert 0 < result["fit_score"] < 100
    assert "Experience with Spark or Kafka" in result["gaps"]


def test_seniority_and_missing_information(resume: ResumeView) -> None:
    staff = compare_posting(
        posting(seniority="staff", min_years=10),
        resume,
        Config().comparison,
        skills_in_text=skills_in_text,
        frequencies=Counter(),
        corpus_size=1,
    )
    assert staff["dimensions"]["seniority"]["score"] == 0.1

    empty = compare_posting(
        posting("Description forthcoming.", min_years=None),
        resume,
        Config().comparison,
        skills_in_text=skills_in_text,
        frequencies=Counter(),
        corpus_size=1,
    )
    assert empty["fit_score"] is None and empty["band"] == "unknown"


def test_runner_stores_results_privately_and_dashboard_serves_them(tmp_path: Path) -> None:
    private = tmp_path / "private"
    silver = SilverStorage(tmp_path / "silver")
    silver.save("fixture/job-1", posting())
    silver.save("fixture/job-2", posting(job_id="job-2", seniority="senior"))

    runner = ApplicationComparisonRunner(private_root=private, silver_dir=tmp_path / "silver")
    assert runner.run().skipped_reason == "no approved active resume"

    service = ResumeIngestionService(private_root=private)
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(make_pdf(SAMPLE_RESUME))
    result = service.ingest(pdf, activate=True)
    assert result.extraction is not None
    service.repository.approve_review(
        result.extraction.extraction_id,
        ReviewApproval(
            revision=1, facts=result.extraction.facts, approved_sections=list(ReviewSection)
        ),
        profile_id=service.profile_id,
    )

    outcome = runner.run()

    assert outcome.compared == 2 and outcome.output_path is not None
    assert outcome.output_path.is_relative_to(private)
    stored = load_results(private)
    assert stored is not None and set(stored["jobs"]) == {"job-1", "job-2"}  # type: ignore[arg-type]

    mart = {"opportunities": [{"job_id": "job-1"}, {"job_id": "unknown"}]}
    merged = attach_application_fit(mart, stored)
    assert merged["opportunities"][0]["application_fit"]["fit_score"] is not None
    assert "application_fit" not in merged["opportunities"][1]
    assert "evidence" not in json.dumps(merged)  # the list never carries resume evidence

    server = create_server(
        port=0,
        mart_path=tmp_path / "missing.json",
        tracking_path=tmp_path / "tracking.json",
        gold_root=tmp_path / "gold",
        private_root=private,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/comparisons/job-1") as response:  # noqa: S310
            payload = json.loads(response.read())
    finally:
        server.shutdown()
        server.server_close()
    assert payload["comparison"]["job_id"] == "job-1"
    assert payload["resume"]["resume_version_id"] == result.resume_version.resume_version_id


def test_extract_requirements_dedupes_and_limits() -> None:
    repeated = posting(
        qualifications=["Python and SQL"] * 3 + [f"Skill line {i} with SQL" for i in range(50)]
    )
    requirements = extract_requirements(repeated, skills_in_text, limit=10)
    assert len(requirements) == 10
    assert [item.text for item in requirements].count("Python and SQL") == 1


def test_years_on_skills_boilerplate_and_keyword_only_postings(resume: ResumeView) -> None:
    text = (
        "What We're Looking For\n"
        "8+ years of Python experience\n"
        "At Example Health, we know great candidates come from many backgrounds\n"
        "Please be ready to discuss a project you led\n"
    )
    lines = [line for line, _ in requirement_lines(posting(text))]
    assert lines == ["8+ years of Python experience"]

    result = compare_posting(
        posting(text),
        resume,
        Config().comparison,
        skills_in_text=skills_in_text,
        frequencies=Counter(),
        corpus_size=1,
    )
    item = result["dimensions"]["requirements"]["items"][0]  # type: ignore[index]
    assert item["status"] == "partial" and item["needed_years"] == 8

    keyword_only = compare_posting(
        posting("We use Python and SQL every day.", min_years=None),
        resume,
        Config().comparison,
        skills_in_text=skills_in_text,
        frequencies=Counter(),
        corpus_size=1,
    )
    assert keyword_only["band"] != "strong" and keyword_only["note"]


def test_bare_degree_word_needs_a_requirement_reading() -> None:
    assert classify("A degree in Statistics or Economics", True, skills_in_text).kind == "degree"
    assert (
        classify("Relevant degree or equivalent experience", True, skills_in_text).kind == "degree"
    )
    program = classify("The program supports degree completion for interns", True, skills_in_text)
    assert program.kind == "text"
