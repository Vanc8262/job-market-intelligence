"""Rule-based resume parsing with exact evidence spans."""

from __future__ import annotations

import pytest

from models.candidate import ExtractedFacts, SkillSource, YearMonth
from profiles.documents import normalize_text
from profiles.parser import ResumeParser, SkillVocabulary
from tests.support.resumes import SAMPLE_RESUME
from utils.config import Config


@pytest.fixture(scope="module")
def parser() -> ResumeParser:
    config = Config()
    return ResumeParser(SkillVocabulary(config.skills, config.resume_profile.skills))


def _assert_spans_match(text: str, facts: ExtractedFacts) -> None:
    for skill in facts.skills:
        for evidence in skill.evidence:
            assert text[evidence.start : evidence.end] == evidence.text
    for item in [*facts.experience, *facts.education, *facts.projects]:
        assert item.evidence is not None
        assert text[item.evidence.start : item.evidence.end] == item.evidence.text


def test_sample_resume_facts(parser: ResumeParser) -> None:
    text = normalize_text(SAMPLE_RESUME)
    result = parser.parse(text)
    facts = result.facts

    assert result.warnings == []
    assert result.sections_found == ["skills", "experience", "education", "projects"]
    _assert_spans_match(text, facts)

    skills = {skill.canonical or skill.name: skill for skill in facts.skills}
    assert {"Python", "SQL", "R", "Java", "pandas", "NumPy", "scikit-learn", "Airflow"} <= set(
        skills
    )
    assert skills["Airflow"].evidence[0].text == "Apache Airflow"
    assert skills["Python"].source == SkillSource.SKILLS_SECTION
    assert len(skills["Python"].evidence) > 1
    assert skills["PostgreSQL"].source == SkillSource.BODY
    assert skills["pandas"].category == "data_science"

    first, second = facts.experience
    assert (first.title, first.employer, first.location) == (
        "Data Analyst Intern",
        "Sunshine Health Analytics",
        "Fort Lauderdale, FL",
    )
    assert (first.start, first.end, first.current) == (
        YearMonth(year=2025, month=5),
        YearMonth(year=2025, month=8),
        False,
    )
    assert len(first.highlights) == 2
    assert (second.title, second.employer, second.current, second.end) == (
        "Software Developer",
        "Coastal Logistics Co.",
        True,
        None,
    )

    fau, pbsc = facts.education
    assert (fau.institution, fau.degree, fau.field_of_study) == (
        "Florida Atlantic University",
        "B.S.",
        "Data Science and Analytics",
    )
    assert (fau.graduation, fau.expected) == (YearMonth(year=2027, month=5), True)
    assert (pbsc.institution, pbsc.degree, pbsc.expected) == (
        "Palm Beach State College",
        "Associate in Arts",
        False,
    )

    tracker, housing = facts.projects
    assert tracker.name == "Job Market Tracker"
    assert tracker.technologies == ["Python", "pandas", "Airflow"]
    assert housing.name == "Housing Price Model"
    assert "statistics" in housing.technologies


def test_alternative_layouts_and_date_formats(parser: ResumeParser) -> None:
    text = normalize_text(
        """Work History
Research Assistant at Ocean Data Lab, Remote 09/2024 - 12/2024
- Cleaned survey data with R and SQL.
Barista, Corner Cafe 2021 - 2023
Skills:
CI/CD, Spark, Kubernetes, Excel
Academic Background
Master of Science in Statistics, University of Somewhere 2026
"""
    )
    facts = parser.parse(text).facts
    _assert_spans_match(text, facts)

    research, barista = facts.experience
    assert (research.title, research.employer, research.location) == (
        "Research Assistant",
        "Ocean Data Lab",
        "Remote",
    )
    assert (research.start, research.end) == (
        YearMonth(year=2024, month=9),
        YearMonth(year=2024, month=12),
    )
    assert (barista.title, barista.employer, barista.start) == (
        "Barista",
        "Corner Cafe",
        YearMonth(year=2021),
    )
    names = {skill.name for skill in facts.skills}
    assert "CI/CD" in names
    assert {"Spark", "Kubernetes"} <= {skill.canonical for skill in facts.skills}
    education = facts.education[0]
    assert (education.degree, education.field_of_study) == ("Master of Science", "Statistics")


def test_body_matching_avoids_ambiguous_words(parser: ResumeParser) -> None:
    text = normalize_text(
        """Experience
Product Associate | Example Co | Miami, FL  Jan 2024 - Present
- Helped spark interest in R&D projects; go-to contact for C-level reports.
"""
    )
    canonicals = {skill.canonical for skill in parser.parse(text).facts.skills}

    assert "Spark" not in canonicals
    assert "R" not in canonicals
    assert "Go" not in canonicals


def test_missing_sections_produce_warnings_not_errors(parser: ResumeParser) -> None:
    result = parser.parse("Jane Doe\nSome text without any headings at all.")

    assert result.facts.experience == []
    assert len(result.warnings) == 4
    assert all("No " in warning for warning in result.warnings)


def test_experience_without_dates_is_reported(parser: ResumeParser) -> None:
    result = parser.parse("Experience\nData Analyst, Example Co\n- Built dashboards\n")

    assert result.facts.experience == []
    assert any("no date ranges" in warning for warning in result.warnings)
