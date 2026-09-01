"""Canonical job posting schema for all future JMIP extractors."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationInfo,
    field_validator,
    model_validator,
)

from utils.hashing import content_hash, duplicate_detection_hash, generate_job_id


class SourceType(StrEnum):
    """Supported source categories."""

    ATS = "ats"
    JOB_BOARD = "job_board"
    PROFESSIONAL_NETWORK = "professional_network"
    HISTORICAL_DATASET = "historical_dataset"
    GOVERNMENT = "government"
    ACADEMIC = "academic"
    OTHER = "other"


class PostingStatus(StrEnum):
    """Lifecycle status for a posting in the pipeline."""

    ACTIVE = "active"
    EXPIRED = "expired"
    REMOVED = "removed"
    DUPLICATE = "duplicate"
    UNKNOWN = "unknown"


class EmploymentType(StrEnum):
    """Supported employment classifications."""

    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    TEMPORARY = "temporary"
    INTERNSHIP = "internship"
    UNKNOWN = "unknown"


class WorkplaceType(StrEnum):
    """Supported workplace arrangements."""

    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class PayPeriod(StrEnum):
    """Supported salary pay periods."""

    HOURLY = "hourly"
    MONTHLY = "monthly"
    YEARLY = "yearly"
    UNKNOWN = "unknown"


class ExperienceLevel(StrEnum):
    """Normalized experience bands used for trend analysis."""

    ENTRY = "entry"
    MID = "mid"
    SENIOR = "senior"
    STAFF = "staff"
    MANAGER = "manager"
    DIRECTOR = "director"
    EXECUTIVE = "executive"
    UNKNOWN = "unknown"


class DegreeLevel(StrEnum):
    """Normalized education levels found in postings."""

    NONE = "none"
    HIGH_SCHOOL = "high_school"
    ASSOCIATE = "associate"
    BACHELOR = "bachelor"
    MASTER = "master"
    DOCTORATE = "doctorate"
    UNKNOWN = "unknown"


class SourceInfo(BaseModel):
    """Information about the source that produced a posting."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    source_type: SourceType = SourceType.OTHER
    url: HttpUrl | None = None
    external_id: str | None = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CompanyInfo(BaseModel):
    """Canonical company information."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    normalized_name: str | None = None
    website: HttpUrl | None = None
    industry: str | None = None


class PositionInfo(BaseModel):
    """Canonical position information."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    normalized_title: str | None = None
    family: str | None = None
    seniority: str | None = None


class LocationInfo(BaseModel):
    """Canonical location information."""

    model_config = ConfigDict(extra="forbid")

    raw: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None
    workplace_type: WorkplaceType = WorkplaceType.UNKNOWN
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class SalaryInfo(BaseModel):
    """Canonical compensation information."""

    model_config = ConfigDict(extra="forbid")

    min_amount: float | None = Field(default=None, ge=0)
    max_amount: float | None = Field(default=None, ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    pay_period: PayPeriod = PayPeriod.UNKNOWN
    raw_text: str | None = None

    @field_validator("max_amount")
    @classmethod
    def validate_salary_range(cls, value: float | None, info: ValidationInfo) -> float | None:
        """Ensure the maximum salary is not below the minimum salary."""
        minimum = info.data.get("min_amount")
        if value is not None and minimum is not None and value < minimum:
            raise ValueError("max_amount must be greater than or equal to min_amount")
        return value

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        """Normalize ISO-like currency codes to uppercase."""
        return value.upper()


class DescriptionInfo(BaseModel):
    """Canonical description fields."""

    model_config = ConfigDict(extra="forbid")

    raw_text: str | None = None
    clean_text: str | None = None
    html: str | None = None
    responsibilities: list[str] = Field(default_factory=list)
    qualifications: list[str] = Field(default_factory=list)
    benefits: list[str] = Field(default_factory=list)


class SkillInfo(BaseModel):
    """Skills extracted or supplied for a posting."""

    model_config = ConfigDict(extra="forbid")

    explicit: list[str] = Field(default_factory=list)
    inferred: list[str] = Field(default_factory=list)
    categories: dict[str, list[str]] = Field(default_factory=dict)


class ExperienceInfo(BaseModel):
    """Experience requirements and inferred seniority signals."""

    model_config = ConfigDict(extra="forbid")

    level: ExperienceLevel = ExperienceLevel.UNKNOWN
    min_years: float | None = Field(default=None, ge=0)
    max_years: float | None = Field(default=None, ge=0)
    raw_text: str | None = None

    @field_validator("max_years")
    @classmethod
    def validate_year_range(cls, value: float | None, info: ValidationInfo) -> float | None:
        """Ensure maximum years are not below minimum years."""
        minimum = info.data.get("min_years")
        if value is not None and minimum is not None and value < minimum:
            raise ValueError("max_years must be greater than or equal to min_years")
        return value


class EducationInfo(BaseModel):
    """Education and certification requirements extracted from a posting."""

    model_config = ConfigDict(extra="forbid")

    minimum_degree: DegreeLevel = DegreeLevel.UNKNOWN
    preferred_degrees: list[DegreeLevel] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    raw_text: str | None = None


class PostingMetadata(BaseModel):
    """Operational metadata about a posting record."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    content_hash: str | None = None
    duplicate_hash: str | None = None
    ingestion_batch_id: str | None = None
    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    posted_at: date | None = None
    expires_at: date | None = None
    status: PostingStatus = PostingStatus.UNKNOWN
    is_active: bool = True
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_dates_and_status(self) -> PostingMetadata:
        """Keep temporal metadata and active flags internally consistent."""
        if (
            self.expires_at is not None
            and self.posted_at is not None
            and self.expires_at < self.posted_at
        ):
            raise ValueError("expires_at must be greater than or equal to posted_at")
        if self.status in {PostingStatus.EXPIRED, PostingStatus.REMOVED, PostingStatus.DUPLICATE}:
            self.is_active = False
        elif self.status == PostingStatus.ACTIVE:
            self.is_active = True
        return self


class RawSourceRecord(BaseModel):
    """Raw payload and source-specific attributes retained for auditability."""

    model_config = ConfigDict(extra="allow")

    raw_url: HttpUrl | None = None
    raw_payload: dict[str, object] = Field(default_factory=dict)
    raw_text: str | None = None


class JobPosting(BaseModel):
    """Canonical schema emitted by every future extractor."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    metadata: PostingMetadata
    source: SourceInfo
    company: CompanyInfo
    position: PositionInfo
    location: LocationInfo = Field(default_factory=LocationInfo)
    salary: SalaryInfo = Field(default_factory=SalaryInfo)
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    experience: ExperienceInfo = Field(default_factory=ExperienceInfo)
    education: EducationInfo = Field(default_factory=EducationInfo)
    description: DescriptionInfo = Field(default_factory=DescriptionInfo)
    skills: SkillInfo = Field(default_factory=SkillInfo)
    raw: RawSourceRecord = Field(default_factory=RawSourceRecord)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_timestamps(self) -> JobPosting:
        """Ensure record update timestamps do not precede creation."""
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must be greater than or equal to created_at")
        return self

    def with_derived_metadata(self) -> JobPosting:
        """Return a copy with stable identity, content, and duplicate hashes populated."""
        location_text = self.location.raw or self.location.city or None
        description_text = self.description.clean_text or self.description.raw_text
        metadata = self.metadata.model_copy(
            update={
                "job_id": self.metadata.job_id
                or generate_job_id(
                    self.source.name,
                    self.company.name,
                    self.position.title,
                    location_text,
                    self.source.external_id,
                ),
                "content_hash": content_hash(
                    description_text,
                    self.position.title,
                    self.company.name,
                ),
                "duplicate_hash": duplicate_detection_hash(
                    self.company.name,
                    self.position.title,
                    location_text,
                ),
            }
        )
        return self.model_copy(update={"metadata": metadata}, deep=True)
