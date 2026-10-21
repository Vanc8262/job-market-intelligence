"""Typed candidate profile, resume version, extraction, and review models.

Extracted facts always carry the exact source span they came from, so a reviewer can
check every fact against the resume text and later models can weigh evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"


def _now() -> datetime:
    return datetime.now(UTC)


class ResumeMediaType(StrEnum):
    """Stored resume formats: uploads are detected from content; TXT is reviewer-edited."""

    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"


class ExtractionStatus(StrEnum):
    """Text-quality outcome for one extraction attempt."""

    PASSED = "passed"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"


class ReviewStatus(StrEnum):
    """Human review state of one extraction's structured facts."""

    PENDING = "pending"
    APPROVED = "approved"


class ReviewSection(StrEnum):
    """Independently reviewed groups of extracted facts."""

    SKILLS = "skills"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    PROJECTS = "projects"


class SkillSource(StrEnum):
    """Where in the resume a skill was found."""

    SKILLS_SECTION = "skills_section"
    BODY = "body"
    REVIEWER = "reviewer"


class CandidateProfile(BaseModel):
    """One local candidate; resume versions and analyses hang off it."""

    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1, max_length=64, pattern=IDENTIFIER_PATTERN)
    display_name: str = Field(min_length=1, max_length=200)
    active_resume_version_id: str | None = Field(default=None, max_length=200)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ResumeVersion(BaseModel):
    """Metadata for one immutable uploaded resume file."""

    model_config = ConfigDict(extra="forbid")

    resume_version_id: str = Field(min_length=1, max_length=200, pattern=IDENTIFIER_PATTERN)
    profile_id: str = Field(min_length=1, max_length=64, pattern=IDENTIFIER_PATTERN)
    label: str = Field(min_length=1, max_length=200)
    original_filename: str = Field(min_length=1, max_length=255)
    media_type: ResumeMediaType
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    storage_key: str = Field(min_length=1, max_length=500)
    uploaded_at: datetime = Field(default_factory=_now)
    derived_from_resume_version_id: str | None = Field(
        default=None,
        max_length=200,
        description="Set on a version made by editing another version's extracted text",
    )


class Evidence(BaseModel):
    """An exact span of the extracted resume text supporting one fact."""

    model_config = ConfigDict(extra="forbid")

    section: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=2000)
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_span(self) -> Evidence:
        """Require a non-empty, forward span."""
        if self.end <= self.start:
            raise ValueError("evidence end must be after start")
        return self


class YearMonth(BaseModel):
    """A resume date with year and optional month precision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    year: int = Field(ge=1950, le=2100)
    month: int | None = Field(default=None, ge=1, le=12)

    def label(self) -> str:
        """Return ``YYYY-MM`` or ``YYYY``."""
        return f"{self.year}-{self.month:02d}" if self.month else str(self.year)


class SkillFact(BaseModel):
    """One skill claimed by the resume, mapped to the taxonomy when possible."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    canonical: str | None = Field(default=None, max_length=100)
    category: str | None = Field(default=None, max_length=100)
    source: SkillSource
    evidence: list[Evidence] = Field(default_factory=list, max_length=50)


class ExperienceFact(BaseModel):
    """One employment entry."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    employer: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    start: YearMonth | None = None
    end: YearMonth | None = None
    current: bool = False
    highlights: list[str] = Field(default_factory=list, max_length=40)
    evidence: Evidence | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> ExperienceFact:
        """Reject an end before the start and an end on a current role."""
        if self.current and self.end is not None:
            raise ValueError("a current role cannot have an end date")
        if (
            self.start
            and self.end
            and (self.end.year, self.end.month or 12)
            < (
                self.start.year,
                self.start.month or 1,
            )
        ):
            raise ValueError("experience end date is before its start date")
        return self


class EducationFact(BaseModel):
    """One degree, diploma, or certificate program."""

    model_config = ConfigDict(extra="forbid")

    institution: str = Field(min_length=1, max_length=200)
    degree: str | None = Field(default=None, max_length=200)
    field_of_study: str | None = Field(default=None, max_length=200)
    gpa: str | None = Field(default=None, max_length=20)
    graduation: YearMonth | None = None
    expected: bool = False
    evidence: Evidence | None = None


class ProjectFact(BaseModel):
    """One portfolio, academic, or work project."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    url: str | None = Field(default=None, max_length=500)
    technologies: list[str] = Field(default_factory=list, max_length=40)
    evidence: Evidence | None = None


class QualityCheck(BaseModel):
    """One evaluated text-quality threshold."""

    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    value: float
    threshold: float
    comparison: Literal["min", "max"]
    message: str


class ExtractedFacts(BaseModel):
    """The four reviewable fact sections."""

    model_config = ConfigDict(extra="forbid")

    skills: list[SkillFact] = Field(default_factory=list, max_length=300)
    experience: list[ExperienceFact] = Field(default_factory=list, max_length=60)
    education: list[EducationFact] = Field(default_factory=list, max_length=30)
    projects: list[ProjectFact] = Field(default_factory=list, max_length=60)

    def section_counts(self) -> dict[str, int]:
        """Return the number of facts per section."""
        return {
            ReviewSection.SKILLS.value: len(self.skills),
            ReviewSection.EXPERIENCE.value: len(self.experience),
            ReviewSection.EDUCATION.value: len(self.education),
            ReviewSection.PROJECTS.value: len(self.projects),
        }


class ResumeExtraction(BaseModel):
    """One parser run over one resume version, plus its human review state."""

    model_config = ConfigDict(extra="forbid")

    extraction_id: str = Field(min_length=1, max_length=200, pattern=IDENTIFIER_PATTERN)
    resume_version_id: str = Field(min_length=1, max_length=200)
    parser_name: str
    parser_version: str
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int | None = Field(default=None, ge=0)
    character_count: int = Field(ge=0)
    quality_score: float = Field(ge=0, le=1)
    quality_checks: list[QualityCheck] = Field(default_factory=list)
    status: ExtractionStatus
    facts: ExtractedFacts = Field(default_factory=ExtractedFacts)
    warnings: list[str] = Field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.PENDING
    approved_sections: list[ReviewSection] = Field(default_factory=list)
    review_comment: str = Field(default="", max_length=5000)
    revision: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=_now)
    review_updated_at: datetime | None = None
    approved_at: datetime | None = None
    superseded: bool = False
    superseded_at: datetime | None = None
    revised_from_extraction_id: str | None = Field(
        default=None,
        max_length=200,
        description="Set when this extraction copies another one's facts for correction",
    )

    @field_validator("approved_sections")
    @classmethod
    def unique_sections(cls, value: list[ReviewSection]) -> list[ReviewSection]:
        """Keep approved sections unique and in canonical order."""
        return [section for section in ReviewSection if section in set(value)]

    @property
    def can_review(self) -> bool:
        """Return whether a reviewer may still change this extraction."""
        return (
            self.review_status == ReviewStatus.PENDING
            and self.status != ExtractionStatus.FAILED
            and not self.superseded
        )


class ReviewDraft(BaseModel):
    """A reviewer's corrected facts and per-section approvals, not yet final."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1, description="Extraction revision the draft was based on")
    facts: ExtractedFacts
    approved_sections: list[ReviewSection] = Field(default_factory=list)
    review_comment: str = Field(default="", max_length=5000)


class ReviewApproval(ReviewDraft):
    """A final review: every section must be approved."""

    @model_validator(mode="after")
    def require_all_sections(self) -> ReviewApproval:
        """Refuse approval until all four sections are explicitly approved."""
        missing = [
            section.value for section in ReviewSection if section not in self.approved_sections
        ]
        if missing:
            raise ValueError(f"approve every section first; missing: {', '.join(missing)}")
        return self


__all__ = [
    "CandidateProfile",
    "EducationFact",
    "Evidence",
    "ExperienceFact",
    "ExtractedFacts",
    "ExtractionStatus",
    "ProjectFact",
    "QualityCheck",
    "ResumeExtraction",
    "ResumeMediaType",
    "ResumeVersion",
    "ReviewApproval",
    "ReviewDraft",
    "ReviewSection",
    "ReviewStatus",
    "SkillFact",
    "SkillSource",
    "YearMonth",
]
