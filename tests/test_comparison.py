"""Phase 6.3: application comparison against an approved (synthetic) resume."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

import pytest

from comparison import RequirementVocabulary, ResumeView, compare_posting, extract_requirements
from comparison.compare import DEGREE_NAMES, assess_requirement
from comparison.requirements import classify, content_text, requirement_lines
from comparison.runner import ApplicationComparisonRunner, load_results
from comparison.vocabulary import stem
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
from utils.config import ComparisonBoilerplateConfig, Config

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


WORKDAY_TAIL = """What You'll Bring
3+ years of experience with SQL
Primary Location Base Pay Range: $100,000 USD - $150,000 USD
Customer service obsession, with a desire to go
above and beyond
to satisfy customer needs.
If you need an accommodation during the hiring process, please let us know
as far in advance as possible.
Our Approach to Flexible Work
With Flex Work, we spend at least half of our time each quarter in the office
with our customers, prospects, and partners (depending on role).
Pursuant to applicable Fair Chance law, we consider applicants with arrest and conviction records.
Are you being referred to one of our roles? Ask about our Employee Referral process!
In addition, Example Health will never ask candidates to pay a recruiting fee.
"""


def test_configured_boilerplate_and_wrapped_lines() -> None:
    """Lines the owner marked "not a requirement" (⊘) in the Labels tab stay out."""
    vocabulary = RequirementVocabulary(Config().comparison)

    lines = requirement_lines(posting(WORKDAY_TAIL), vocabulary)

    assert lines == [
        ("3+ years of experience with SQL", True),
        (
            "Customer service obsession, with a desire to go above and beyond to satisfy "
            "customer needs.",
            True,
        ),
    ]
    # Without the vocabulary only the built-in rules apply, so the fragments survive.
    bare = [text for text, _ in requirement_lines(posting(WORKDAY_TAIL))]
    assert "as far in advance as possible." in bare
    assert "to satisfy customer needs." in bare


def test_boilerplate_phrases_apply_to_lever_and_flat_text() -> None:
    vocabulary = RequirementVocabulary(Config().comparison)
    lever = posting(
        qualifications=["Python and SQL", "We will never ask applicants to pay a recruiting fee"]
    )
    flat = posting(
        "Candidates need 3+ years of experience with Python. "
        "Applicants with arrest and conviction records will be considered under Fair Chance law."
    )

    assert requirement_lines(lever, vocabulary) == [("Python and SQL", True)]
    assert [text for text, _ in requirement_lines(flat, vocabulary)] == [
        "Candidates need 3+ years of experience with Python."
    ]


def test_a_continuation_starts_only_after_an_unfinished_line() -> None:
    vocabulary = RequirementVocabulary(Config().comparison)
    text = (
        "Requirements:\n"
        "experience with dbt and Airflow\n"
        "Strong SQL skills.\n"
        "familiarity with Looker or Tableau\n"
    )

    lines = [line for line, _ in requirement_lines(posting(text), vocabulary)]

    # A lowercase line after a heading or a finished sentence is its own requirement.
    assert lines == [
        "experience with dbt and Airflow",
        "Strong SQL skills.",
        "familiarity with Looker or Tableau",
    ]


def test_boilerplate_patterns_are_validated() -> None:
    with pytest.raises(ValueError, match="invalid boilerplate pattern"):
        ComparisonBoilerplateConfig(phrases=["(unclosed"])


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


def _assess(line: str, resume: ResumeView, *, required: bool = True) -> dict[str, object]:
    settings = Config().comparison
    vocabulary = RequirementVocabulary(settings)
    requirement = classify(line, required, skills_in_text, vocabulary)
    return assess_requirement(requirement, resume, settings, vocabulary)


def test_plain_language_concepts_are_judged_on_meaning(resume: ResumeView) -> None:
    apis = _assess("Comfort working across APIs, data pipelines, and cloud infrastructure", resume)
    assert apis["kind"] == "concept" and apis["status"] == "met"
    evidence = {item["concept"]: item["evidence"] for item in apis["concepts"]}  # type: ignore[attr-defined]
    assert evidence["apis_integrations"]["term"] == "APIs"  # type: ignore[index]
    assert "AWS" in evidence["cloud_infrastructure"]["text"]  # type: ignore[index]

    mixed = _assess(
        "Familiarity with data structures, storage systems, and front-end frameworks", resume
    )
    met = {item["concept"]: item["met"] for item in mixed["concepts"]}  # type: ignore[attr-defined]
    assert met == {
        "data_structures_algorithms": True,  # shown by the data science degree
        "storage_databases": True,
        "frontend": False,  # the synthetic resume has no front-end work
    }
    assert mixed["status"] == "partial"


def test_traits_are_self_assessed_and_left_out_of_coverage(resume: ResumeView) -> None:
    trait = _assess("You approach challenges with curiosity and find solutions.", resume)
    assert trait["kind"] == "trait" and trait["status"] == "self_assess"
    assert trait["weight"] == 0.0

    text = POSTING_TEXT.replace(
        "Excellent communication with clinical stakeholders",
        "Understanding of how technical decisions impact the user of what you're building",
    )
    result = compare_posting(
        posting(text),
        resume,
        Config().comparison,
        skills_in_text=skills_in_text,
        frequencies=Counter(),
        corpus_size=1,
    )
    requirements = result["dimensions"]["requirements"]  # type: ignore[index]
    assert requirements["self_assess"] == 1
    assert not any("impact the user" in gap for gap in result["gaps"])  # type: ignore[attr-defined]


def test_related_fields_of_study_count(resume: ResumeView) -> None:
    background = _assess(
        "Strong engineering background, preferred in fields such as Computer Science, "
        "Mathematics, Software Engineering, Physics",
        resume,
        required=False,
    )
    assert background["kind"] == "degree" and background["status"] == "met"
    assert background["field_match"] == "related"  # B.S. Data Science and Analytics
    # A specific unrelated field is missing; a broad one ("engineering") stays partial.
    other = _assess("Bachelor's degree in Electrical Engineering", resume)
    assert other["status"] == "missing" and other["field_match"] == "none"
    broad = _assess("Bachelor's degree in engineering", resume)
    assert broad["status"] == "partial" and broad["field_match"] == "none"


def test_multi_level_degree_lines_need_the_lowest_level_named(resume: ResumeView) -> None:
    line = _assess("B.S., M.S., or Ph.D. in Data Science, Statistics, or a related field", resume)
    assert line["kind"] == "degree" and line["status"] == "met"
    assert line["needed_degree"] == DEGREE_NAMES[3]
    masters = _assess("Master's or PhD in Statistics", resume)
    assert masters["status"] == "missing"


def test_years_lines_naming_a_role_count_only_roles_that_show_it(resume: ResumeView) -> None:
    # About 4 years in total, none of it as a product manager ("3 product teams" alone
    # is one shared term, below min_shared_terms).
    product = _assess("4+ years of experience as a Product Manager", resume)
    assert product["kind"] == "years" and product["status"] == "missing"
    assert product["resume_years"] == 0
    assert product["evidence"] == {"roles": []}
    developer = _assess("3+ years of experience as a software developer", resume)
    assert developer["status"] == "met"
    assert developer["evidence"]["roles"][0].startswith("Software Developer")  # type: ignore[index]
    generic = _assess("3+ years of relevant professional experience", resume)
    assert generic["status"] == "met" and "subject" not in generic


def test_stemming_and_synonyms_match_different_wording() -> None:
    vocabulary = RequirementVocabulary(Config().comparison)
    assert stem("pipelines") == stem("pipeline")
    assert stem("databases") == stem("database")
    assert stem("reliability") == stem("reliable")
    assert vocabulary.canonical("ETL") == vocabulary.canonical("pipelines")
    assert vocabulary.canonical("troubleshooting") == vocabulary.canonical("debug")
    assert vocabulary.canonical("built") == vocabulary.canonical("implementing")
    view = ResumeView.from_facts(
        ResumeParser(SkillVocabulary(Config().skills, [])).parse(SAMPLE_RESUME).facts,
        SAMPLE_RESUME + "\nTroubleshot failing ETL jobs and set up alerting for them.\n",
    )
    text = _assess("Experience debugging data workflows and monitoring their health", view)
    assert text["kind"] == "text" and text["status"] == "met"
    assert "debugging (troubleshot)" in text["matched_terms"]  # type: ignore[operator]


def test_named_products_count_more_than_comparable_experience(resume: ResumeView) -> None:
    foundry = _assess(
        "Hands-on experience with Foundry, Contour, or comparable data and analytics platforms",
        resume,
    )
    assert [item["name"] for item in foundry["named"]] == ["Foundry", "Contour"]  # type: ignore[attr-defined]
    assert foundry["status"] == "partial" and foundry["score"] == 0.6

    airflow = _assess("Experience with Airflow, Dagster, or similar orchestration tools", resume)
    assert airflow["status"] == "met"  # Airflow is on the resume

    generic = _assess("Experience with APIs, cloud, or similar", resume)
    assert "named" not in generic and generic["status"] == "met"  # concepts, not products


def test_equivalent_alternatives_count_fully_and_named_products_join_keywords(
    resume: ResumeView,
) -> None:
    another = _assess(
        "Fluency in TypeScript, or another general-purpose programming language", resume
    )
    assert another["alternatives"] == "equivalent" and another["status"] == "met"  # Python
    comparable = _assess("Experience with RocksDB or similar data stores", resume)
    assert comparable["alternatives"] == "comparable" and comparable["status"] == "partial"

    text = POSTING_TEXT.replace(
        "Experience with Spark or Kafka",
        "Hands-on experience with Foundry, Contour, or comparable analytics platforms",
    )
    result = compare_posting(
        posting(text),
        resume,
        Config().comparison,
        skills_in_text=skills_in_text,
        frequencies=Counter(),
        corpus_size=1,
    )
    assert [item["skill"] for item in result["named_to_add"]] == ["Foundry", "Contour"]  # type: ignore[attr-defined]
