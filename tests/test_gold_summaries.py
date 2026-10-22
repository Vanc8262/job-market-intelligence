"""Tests for analytics-ready Silver-to-Gold summaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from analytics import SilverToGoldRunner
from models.job_posting import (
    CompanyInfo,
    DescriptionInfo,
    EmploymentType,
    JobPosting,
    LocationInfo,
    PayPeriod,
    PositionInfo,
    PostingMetadata,
    PostingStatus,
    SalaryInfo,
    SkillInfo,
    SourceInfo,
    SourceType,
    WorkplaceType,
)
from models.lifecycle import LifecycleEntry, LifecycleState, LifecycleStatus
from storage import GoldStorage, SilverStorage
from utils.config import Config


def posting(
    job_id: str,
    *,
    title: str,
    normalized_title: str,
    family: str,
    employer: str,
    location: str,
    region: str,
    workplace: WorkplaceType,
    skills: list[str],
    salary: tuple[float, float] | None = None,
    status: PostingStatus = PostingStatus.ACTIVE,
) -> JobPosting:
    """Return a compact normalized Silver fixture."""
    salary_info = (
        SalaryInfo(
            min_amount=salary[0],
            max_amount=salary[1],
            currency="USD",
            pay_period=PayPeriod.YEARLY,
        )
        if salary
        else SalaryInfo()
    )
    return JobPosting(
        metadata=PostingMetadata(job_id=job_id, status=status),
        source=SourceInfo(
            name="fixture",
            source_type=SourceType.ATS,
            external_id=job_id,
            url=f"https://jobs.example.com/{job_id}",
        ),
        company=CompanyInfo(name=employer, normalized_name=employer),
        position=PositionInfo(
            title=title,
            normalized_title=normalized_title,
            family=family,
            seniority="mid" if "Engineer" in normalized_title else None,
        ),
        location=LocationInfo(
            raw=location,
            region=region,
            country="United States",
            workplace_type=workplace,
        ),
        salary=salary_info,
        description=DescriptionInfo(clean_text=f"Use {' and '.join(skills)}."),
        skills=SkillInfo(inferred=skills, categories={"fixture": skills}),
    ).with_derived_metadata()


def build_runner(
    tmp_path: Path,
) -> tuple[SilverToGoldRunner, SilverStorage, GoldStorage]:
    """Build a Gold runner with isolated local layers.

    Role tiers, avoided titles, and employment preferences are switched off so these
    tests pin the core scoring mechanics; preference behavior is tested separately.
    """
    silver = SilverStorage(tmp_path / "silver")
    gold = GoldStorage(tmp_path / "gold")
    config = Config()
    profile = config.resume_profile.model_copy(
        update={"title_tiers": [], "avoid_title_keywords": [], "preferred_employment_types": []}
    )
    runner = SilverToGoldRunner(
        config,
        silver_storage=silver,
        gold_storage=gold,
        resume_profile=profile,
    )
    return runner, silver, gold


def test_gold_runner_builds_all_summaries_and_resume_ranking(tmp_path: Path) -> None:
    runner, silver, gold = build_runner(tmp_path)
    silver.save(
        "fixture/data-engineer",
        posting(
            "data-engineer",
            title="Data Engineer II",
            normalized_title="Data Engineer",
            family="data_engineering",
            employer="Example Data",
            location="Remote - United States",
            region="United States",
            workplace=WorkplaceType.REMOTE,
            skills=["Python", "SQL", "AWS"],
            salary=(100_000, 140_000),
        ),
    )
    gold.save(
        "lifecycle/state",
        LifecycleState(
            updated_at=datetime(2026, 8, 25, tzinfo=UTC),
            last_collection_run_id="collection-test",
            entries={
                "data-engineer": LifecycleEntry(
                    target_name="example-data",
                    source_name="fixture",
                    source_reference="data-engineer",
                    job_id="data-engineer",
                    external_id="data-engineer",
                    title="Data Engineer",
                    employer="Example Data",
                    profile_name="fixture-profile",
                    query_names=["data-engineering"],
                    status=LifecycleStatus.ACTIVE,
                    first_seen_at=datetime(2026, 8, 20, tzinfo=UTC),
                    last_seen_at=datetime(2026, 8, 25, tzinfo=UTC),
                    last_changed_at=datetime(2026, 8, 21, tzinfo=UTC),
                    content_hash="fixture-hash",
                    observation_count=2,
                )
            },
        ).model_dump(mode="json"),
    )
    silver.save(
        "fixture/veterinarian",
        posting(
            "veterinarian",
            title="Veterinarian",
            normalized_title="Veterinarian",
            family="Veterinary",
            employer="Example Health",
            location="Orlando, FL",
            region="Florida",
            workplace=WorkplaceType.ONSITE,
            skills=["patient care"],
        ),
    )
    silver.save(
        "fixture/duplicate",
        posting(
            "duplicate",
            title="Software Engineer",
            normalized_title="Software Engineer",
            family="software_engineering",
            employer="Example Data",
            location="Remote",
            region="Florida",
            workplace=WorkplaceType.REMOTE,
            skills=["Java"],
            status=PostingStatus.DUPLICATE,
        ),
    )

    result = runner.run(
        run_id="gold-test",
        generated_at=datetime(2026, 8, 25, tzinfo=UTC),
    )

    assert result.silver_records_found == 3
    assert result.records_analyzed == 2
    assert result.excluded_inactive == 1
    assert result.failed == 0
    assert set(result.outputs) == {
        "skills",
        "titles",
        "employers",
        "locations",
        "salaries",
        "resume_relevance",
        "current_opportunities",
        "manifest",
    }

    skills = gold.load("skills/summary")
    assert skills["coverage"]["postings_with_skills"] == 2
    shrinkage = skills["bayesian"]
    assert shrinkage["model"]["name"] == "skill_shares" and shrinkage["postings"] == 2
    assert all(row["interval"]["low"] <= row["share"] for row in shrinkage["skills"])
    assert skills["skills"][0]["posting_count"] == 1
    assert {row["skill"] for row in skills["skills"]} == {
        "AWS",
        "Python",
        "SQL",
        "patient care",
    }

    employers = gold.load("employers/summary")
    assert employers["distinct_employers"] == 2
    assert employers["employers"][0]["employer"] == "Example Data"

    locations = gold.load("locations/summary")
    workplace = {
        row["workplace_type"]: row["posting_count"] for row in locations["workplace_types"]
    }
    assert workplace == {"onsite": 1, "remote": 1}

    salaries = gold.load("salaries/summary")
    assert salaries["coverage"] == {
        "postings_with_salary": 1,
        "posting_share": 0.5,
        "postings_without_salary": 1,
    }
    assert salaries["currencies"][0]["median_annual_midpoint"] == 120_000

    relevance = gold.load("resume_relevance/summary")
    ranked = relevance["ranked_postings"]
    assert ranked[0]["job_id"] == "data-engineer"
    assert ranked[0]["rank"] == 1
    assert ranked[0]["match_band"] == "strong"
    assert ranked[0]["matched_skills"] == ["Python", "SQL", "AWS"]
    assert ranked[0]["score"] > ranked[1]["score"]
    assert ranked[1]["home_region_match"] is True

    mart = gold.load("current_opportunities/mart")
    assert mart["row_count"] == 2
    assert mart["grain"] == "one row per active nonduplicate canonical job posting"
    assert mart["coverage"]["lifecycle_tracked"] == 1
    opportunity = mart["opportunities"][0]
    assert opportunity["job_id"] == "data-engineer"
    assert opportunity["resume_relevance_score"] == ranked[0]["score"]
    assert opportunity["lifecycle_status"] == "active"
    assert opportunity["lifecycle_target"] == "example-data"
    assert opportunity["age_days"] == 5
    assert opportunity["salary_midpoint"] == 120_000
    assert opportunity["skills"] == ["Python", "SQL", "AWS"]
    assert opportunity["url"] == "https://jobs.example.com/data-engineer"


def test_gold_runner_isolates_invalid_silver_and_has_stable_fingerprint(
    tmp_path: Path,
) -> None:
    runner, silver, gold = build_runner(tmp_path)
    silver.save(
        "fixture/valid",
        posting(
            "valid",
            title="Software Engineer",
            normalized_title="Software Engineer",
            family="software_engineering",
            employer="Example",
            location="Remote",
            region="Florida",
            workplace=WorkplaceType.REMOTE,
            skills=["Java"],
        ),
    )
    invalid = silver.path_for("fixture/invalid", ".json")
    invalid.parent.mkdir(parents=True, exist_ok=True)
    invalid.write_text(json.dumps({"not": "a posting"}), encoding="utf-8")

    first = runner.run(generated_at=datetime(2026, 8, 25, tzinfo=UTC))
    second = runner.run(generated_at=datetime(2026, 8, 26, tzinfo=UTC))
    manifest = gold.load("manifest/latest")

    assert first.silver_records_found == 2
    assert first.records_analyzed == 1
    assert first.failed == 1
    assert first.input_fingerprint == second.input_fingerprint
    assert manifest["invalid_silver_records"] == 1
    assert manifest["failures"][0]["record"] == "fixture/invalid.json"


def test_gold_runner_handles_empty_silver_layer(tmp_path: Path) -> None:
    runner, _, gold = build_runner(tmp_path)

    result = runner.run(generated_at=datetime(2026, 8, 25, tzinfo=UTC))

    assert result.records_analyzed == 0
    assert result.failed == 0
    assert gold.load("skills/summary")["coverage"]["posting_share"] == 0.0
    relevance = gold.load("resume_relevance/summary")
    assert relevance["ranked_postings"] == []
    assert relevance["score_summary"]["average"] is None
    mart = gold.load("current_opportunities/mart")
    assert mart["opportunities"] == []
    assert mart["coverage"]["lifecycle_tracking_share"] == 0.0


def test_relevance_signals_use_evidence_title_boundaries_and_eligibility_caps(
    tmp_path: Path,
) -> None:
    runner, _, _ = build_runner(tmp_path)
    fuzzy_title = posting(
        "fuzzy-title",
        title="Systems Engineer, Data Intelligence and Analytics",
        normalized_title="Systems Engineer, Data Intelligence and Analytics",
        family="Business Intelligence",
        employer="Example",
        location="New York, NY",
        region="New York",
        workplace=WorkplaceType.ONSITE,
        skills=["Python", "SQL", "Java", "AWS"],
    )
    fuzzy_row = runner._relevance_row(fuzzy_title)

    assert fuzzy_row["title_match_type"] == "fuzzy"
    assert fuzzy_row["matched_title_signals"] == []
    assert fuzzy_row["score_components"]["title"]["signal"] == 0.6

    family_variant = posting(
        "family-variant",
        title="Platform Specialist",
        normalized_title="Platform Specialist",
        family="Data Engineering",
        employer="Example",
        location="Remote",
        region="United States",
        workplace=WorkplaceType.REMOTE,
        skills=["Python"],
    )
    family_row = runner._relevance_row(family_variant)
    assert family_row["family_match"] is True
    assert family_row["title_match_type"] == "family"

    inferred = posting(
        "inferred",
        title="Software Engineer",
        normalized_title="Software Engineer",
        family="software_engineering",
        employer="Example",
        location="Remote",
        region="United States",
        workplace=WorkplaceType.REMOTE,
        skills=["Python", "SQL", "Java", "AWS"],
    )
    explicit = inferred.model_copy(deep=True)
    explicit.skills = SkillInfo(explicit=inferred.skills.inferred)
    inferred_row = runner._relevance_row(inferred)
    explicit_row = runner._relevance_row(explicit)
    assert (
        explicit_row["score_components"]["skills"]["signal"]
        > (inferred_row["score_components"]["skills"]["signal"])
    )
    assert explicit_row["skill_match_details"][0]["evidence"] == "explicit"

    nonlocal_onsite = explicit.model_copy(deep=True)
    nonlocal_onsite.location = LocationInfo(
        raw="New York, NY",
        region="New York",
        country="United States",
        workplace_type=WorkplaceType.ONSITE,
    )
    nonlocal_row = runner._relevance_row(nonlocal_onsite)
    assert nonlocal_row["pre_cap_score"] == 85.0
    assert nonlocal_row["score"] == 79.0
    assert nonlocal_row["score_cap_reason"] == "non_home_onsite_without_relocation"
    assert nonlocal_row["match_band"] == "moderate"

    senior = explicit.model_copy(deep=True)
    senior.position.seniority = "senior"
    senior_row = runner._relevance_row(senior)
    assert senior_row["score_components"]["experience"]["signal"] == 0.25


def test_job_search_preferences_shape_relevance(tmp_path: Path) -> None:
    config = Config()
    profile = config.resume_profile.model_copy(
        update={
            "scoring": config.resume_profile.scoring.model_copy(
                update={"non_preferred_employment_score_cap": 79}
            )
        }
    )
    runner = SilverToGoldRunner(
        config,
        silver_storage=SilverStorage(tmp_path / "silver"),
        gold_storage=GoldStorage(tmp_path / "gold"),
        resume_profile=profile,
    )
    skills = ["Python", "SQL", "AWS", "machine learning", "Java"]

    def row(title: str, location: str, workplace: WorkplaceType) -> dict[str, object]:
        return runner._relevance_row(
            posting(
                title.casefold().replace(" ", "-"),
                title=title,
                normalized_title=title,
                family="Other",
                employer="Example",
                location=location,
                region="",
                workplace=workplace,
                skills=skills,
            )
        )

    scientist = row("Data Scientist", "Winter Park, FL, US, 32789", WorkplaceType.ONSITE)
    assert scientist["title_tier"] == "primary"
    assert scientist["home_base"] == "Orlando, FL"
    assert scientist["home_base_miles"] == 6
    assert scientist["score_cap_reason"] is None

    colorado = row("Software Engineer", "Aurora, CO", WorkplaceType.HYBRID)
    assert colorado["title_tier"] == "secondary" and colorado["home_base"] == "Denver, CO"
    assert colorado["score_components"]["title"]["signal"] == 0.85

    # A place name in another state is not a home base: no location credit on-site.
    elsewhere = row("Data Scientist", "Aurora, Ontario, Canada", WorkplaceType.ONSITE)
    assert elsewhere["home_base"] is None and elsewhere["home_region_match"] is False
    assert elsewhere["score_components"]["location"]["signal"] == 0.0

    analyst = row("Data Analyst", "Remote", WorkplaceType.REMOTE)
    assert analyst["title_tier"] == "lower"

    avoided = row("Data Entry Specialist", "Remote", WorkplaceType.REMOTE)
    assert avoided["avoided_title_keywords"] == ["data entry"]
    assert avoided["score_components"]["title"]["signal"] == 0.0
    assert float(str(avoided["score"])) <= profile.scoring.avoided_title_score_cap

    full_time = runner._relevance_row(
        posting(
            "full-time",
            title="Data Scientist",
            normalized_title="Data Scientist",
            family="data_science",
            employer="Example",
            location="Remote",
            region="",
            workplace=WorkplaceType.REMOTE,
            skills=skills,
        ).model_copy(update={"employment_type": EmploymentType.FULL_TIME})
    )
    assert full_time["employment_preferred"] is False
    assert full_time["score_cap_reason"] == "non_preferred_employment_type"
    assert full_time["score"] == 79
