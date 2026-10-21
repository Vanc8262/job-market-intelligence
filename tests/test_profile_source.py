"""The scoring profile takes resume facts from the approved active resume."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from models.candidate import ExperienceFact, ReviewApproval, ReviewSection, YearMonth
from profiles import ResumeIngestionService
from profiles.profile_source import experience_years, resolve_resume_profile
from tests.support.resumes import SAMPLE_RESUME, make_pdf
from utils.config import Config


def test_experience_years_counts_overlaps_once() -> None:
    roles = [
        ExperienceFact(
            title="Intern", start=YearMonth(year=2021, month=7), end=YearMonth(year=2021, month=8)
        ),
        ExperienceFact(
            title="Developer",
            start=YearMonth(year=2021, month=8),
            end=YearMonth(year=2024, month=7),
        ),
        ExperienceFact(title="No dates"),
    ]
    assert experience_years(roles) == 3.08
    current = [ExperienceFact(title="Now", start=YearMonth(year=2025, month=1), current=True)]
    assert experience_years(current, today=datetime(2026, 1, 15, tzinfo=UTC)) == 1.08


def test_profile_uses_the_approved_active_resume(tmp_path: Path) -> None:
    config = Config()
    private = tmp_path / "private"

    profile, source = resolve_resume_profile(config, private_root=private)
    assert source == {"source": "config", "reason": "no approved active resume"}
    assert not private.exists()  # resolving never creates the private store

    service = ResumeIngestionService(config, private_root=private)
    pdf = tmp_path / "jordan.pdf"
    pdf.write_bytes(make_pdf(SAMPLE_RESUME))
    result = service.ingest(pdf, activate=True)
    assert result.extraction is not None
    profile, source = resolve_resume_profile(config, private_root=private)
    assert source["source"] == "config"  # activated but not yet approved

    extraction = result.extraction
    service.repository.approve_review(
        extraction.extraction_id,
        ReviewApproval(
            revision=extraction.revision,
            facts=extraction.facts,
            approved_sections=list(ReviewSection),
        ),
        profile_id=service.profile_id,
    )
    profile, source = resolve_resume_profile(config, private_root=private)

    assert source["source"] == "approved_resume"
    assert source["extraction_id"] == extraction.extraction_id
    expected = {(fact.canonical or fact.name) for fact in extraction.facts.skills}
    assert set(profile.skills) == expected
    assert "Airflow" in " ".join(profile.skills) or "Apache Airflow" in profile.skills
    assert {skill.casefold() for skill in profile.scoring.core_skills} <= {
        skill.casefold() for skill in profile.skills
    }
    # Preferences a resume does not state still come from YAML.
    assert profile.target_titles == config.resume_profile.target_titles
    assert profile.home_bases == config.resume_profile.home_bases
    years = experience_years(extraction.facts.experience)
    assert profile.scoring.maximum_preferred_experience_years == round(years) + 1

    service.repository.set_superseded(extraction.extraction_id, True, profile_id=service.profile_id)
    _, source = resolve_resume_profile(config, private_root=private)
    assert source["source"] == "config"
