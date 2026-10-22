"""Tests for configuration-driven job posting normalization."""

from models.job_posting import (
    CompanyInfo,
    DescriptionInfo,
    JobPosting,
    LocationInfo,
    PayPeriod,
    PositionInfo,
    PostingMetadata,
    SalaryInfo,
    SkillInfo,
    SourceInfo,
    WorkplaceType,
)
from processors import JobPostingNormalizer


def posting_for_normalization() -> JobPosting:
    """Build a posting containing representative unnormalized values."""
    return JobPosting(
        metadata=PostingMetadata(job_id="job-101"),
        source=SourceInfo(name="lever"),
        company=CompanyInfo(name="  Google LLC  "),
        position=PositionInfo(title="  Senior   ML Engineer "),
        location=LocationInfo(raw="Hybrid - New York, NY"),
        salary=SalaryInfo(
            min_amount=70,
            max_amount=90,
            currency="usd",
            pay_period=PayPeriod.HOURLY,
            raw_text="$70-$90 per hour",
        ),
        description=DescriptionInfo(
            clean_text="Build services with python, SQL, JavaScript, PyTorch, and AWS."
        ),
        skills=SkillInfo(explicit=["python", "Custom Tool", "PYTHON"]),
    )


def test_normalize_applies_all_configured_taxonomies_without_mutating_input() -> None:
    original = posting_for_normalization()

    normalized = JobPostingNormalizer().normalize(original)

    assert original.company.normalized_name is None
    assert original.salary.min_amount == 70
    assert normalized.position.normalized_title == "Machine Learning Engineer"
    assert normalized.position.family == "machine_learning"
    assert normalized.position.seniority == "senior"
    assert normalized.company.name == "  Google LLC  "
    assert normalized.company.normalized_name == "Google"
    assert normalized.location.city == "New York"
    assert normalized.location.region == "NY"
    assert normalized.location.country == "United States"
    assert normalized.location.workplace_type == WorkplaceType.HYBRID
    assert normalized.salary.min_amount == 145600
    assert normalized.salary.max_amount == 187200
    assert normalized.salary.pay_period == PayPeriod.YEARLY


def test_skill_normalization_canonicalizes_deduplicates_and_categorizes() -> None:
    normalized = JobPostingNormalizer().normalize(posting_for_normalization())

    assert normalized.skills.explicit == ["Python", "Custom Tool"]
    # "ML" in the title is the exact-case alias of machine learning.
    assert normalized.skills.inferred == [
        "SQL",
        "JavaScript",
        "machine learning",
        "PyTorch",
        "AWS",
    ]
    assert normalized.skills.categories == {
        "programming_languages": ["Python", "SQL", "JavaScript"],
        "machine_learning": ["machine learning", "PyTorch"],
        "cloud": ["AWS"],
    }
    assert "Java" not in normalized.skills.inferred


def test_salary_normalization_parses_compact_annual_range_and_currency() -> None:
    posting = posting_for_normalization()
    posting.salary = SalaryInfo(raw_text="CAD 120K - 150K annually")

    normalized = JobPostingNormalizer().normalize(posting)

    assert normalized.salary.min_amount == 120000
    assert normalized.salary.max_amount == 150000
    assert normalized.salary.currency == "CAD"
    assert normalized.salary.pay_period == PayPeriod.YEARLY


def test_salary_normalization_annualizes_monthly_values() -> None:
    posting = posting_for_normalization()
    posting.salary = SalaryInfo(
        min_amount=10000,
        max_amount=12000,
        pay_period=PayPeriod.MONTHLY,
    )

    normalized = JobPostingNormalizer().normalize(posting)

    assert normalized.salary.min_amount == 120000
    assert normalized.salary.max_amount == 144000
    assert normalized.salary.pay_period == PayPeriod.YEARLY


def test_salary_raw_period_annualizes_structured_amounts() -> None:
    posting = posting_for_normalization()
    posting.salary = SalaryInfo(
        min_amount=50,
        max_amount=60,
        pay_period=PayPeriod.UNKNOWN,
        raw_text="$50-$60 hourly",
    )

    normalized = JobPostingNormalizer().normalize(posting)

    assert normalized.salary.min_amount == 104000
    assert normalized.salary.max_amount == 124800
    assert normalized.salary.pay_period == PayPeriod.YEARLY


def test_company_suffix_and_remote_only_location_are_normalized_safely() -> None:
    posting = posting_for_normalization()
    posting.company.name = "Example Analytics, Inc."
    posting.location = LocationInfo(raw="Remote")

    normalized = JobPostingNormalizer().normalize(posting)

    assert normalized.company.normalized_name == "Example Analytics"
    assert normalized.location.workplace_type == WorkplaceType.REMOTE
    assert normalized.location.city is None
    assert normalized.location.country is None


def test_remote_country_and_three_part_location_parse_without_city_corruption() -> None:
    remote_posting = posting_for_normalization()
    remote_posting.location = LocationInfo(raw="Remote - United States")
    onsite_posting = posting_for_normalization()
    onsite_posting.location = LocationInfo(raw="San Francisco, CA, United States")

    remote = JobPostingNormalizer().normalize(remote_posting)
    onsite = JobPostingNormalizer().normalize(onsite_posting)

    assert remote.location.city is None
    assert remote.location.country == "United States"
    assert onsite.location.city == "San Francisco"
    assert onsite.location.region == "CA"
    assert onsite.location.country == "United States"


def test_normalization_is_idempotent_and_preserves_raw_record() -> None:
    posting = posting_for_normalization()
    posting.raw.raw_payload = {"source_title": "  Senior   ML Engineer "}

    first = JobPostingNormalizer().normalize(posting)
    second = JobPostingNormalizer().normalize(first)

    assert second == first
    assert second.raw == posting.raw


def _skills_for(text: str, title: str = "Software Engineer") -> list[str]:
    posting = posting_for_normalization().model_copy(
        update={
            "position": PositionInfo(title=title),
            "description": DescriptionInfo(clean_text=text),
            "skills": SkillInfo(),
        }
    )
    return JobPostingNormalizer().normalize(posting).skills.inferred


def test_go_needs_programming_context() -> None:
    real = [
        "Strong programming skills in Python, Go, Scala, Java, or similar languages.",
        "Deep experience using Go in production environments.",
        "Contribute to runtime ecosystems such as Go and tackle novel problems.",
        "Nice-to-haves include Golang, Terraform, and EKS.",
        "Systems-level programming experience in Rust, C, or C++, ideally Go.",
    ]
    everyday = [
        "Partner with our go-to-market organization on pricing.",
        "Guide customers from discovery through go-live and optimisation.",
        "A desire to go above and beyond for every customer.",
        "Speed and quality go hand in hand.",
        "The BT Go-To-Market (GTM) Platform team owns forecasting.",
        "You can expect to: Go onsite and meet with customer analysts.",
    ]
    for text in real:
        assert "Go" in _skills_for(text), text
    for text in everyday:
        assert "Go" not in _skills_for(text), text


def test_short_aliases_match_exact_case_only() -> None:
    assert "machine learning" in _skills_for("You will own ML systems end to end.")
    assert "LLMs" in _skills_for("Build LLM evaluation harnesses.")
    assert "machine learning" not in _skills_for("Tickets are 1 ml of paperwork.")
