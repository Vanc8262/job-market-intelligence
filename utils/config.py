"""Centralized YAML configuration loader with validation and environment overrides."""

from __future__ import annotations

import os
import re
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Self, TypeVar, cast

import yaml
from dotenv import load_dotenv
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

TConfigModel = TypeVar("TConfigModel", bound=BaseModel)
LOCAL_RESUME_PROFILE = "resume_profile.local.yaml"
PIPELINE_DATA_FOLDERS = {
    "bronze_dir": "bronze",
    "silver_dir": "silver",
    "gold_dir": "gold",
    "checkpoint_dir": "checkpoints",
}


class ConfigError(RuntimeError):
    """Raised when configuration cannot be loaded or validated."""


class ProjectConfig(BaseModel):
    """Project metadata configuration."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str


class PathConfig(BaseModel):
    """Filesystem paths used by the platform."""

    model_config = ConfigDict(extra="forbid")

    data_dir: Path
    bronze_dir: Path
    silver_dir: Path
    gold_dir: Path
    cache_dir: Path
    checkpoint_dir: Path
    log_dir: Path
    private_dir: Path = Path("data/private")

    def anchored(self, project_root: Path) -> PathConfig:
        """Return a copy with relative paths resolved against the project root.

        Scripts can then be launched from any working directory without writing
        data or logs into that directory.
        """
        values = {
            name: value if value.is_absolute() else project_root / value
            for name, value in self.model_dump().items()
        }
        return PathConfig.model_validate(values)


class StorageConfig(BaseModel):
    """Default storage behavior."""

    model_config = ConfigDict(extra="forbid")

    default: str = "parquet"
    compression: str = "zstd"
    partition_by: list[str] = Field(default_factory=list)


class LoggingLevelConfig(BaseModel):
    """Application logging level setting."""

    model_config = ConfigDict(extra="forbid")

    level: str = "INFO"


class ScrapingConfig(BaseModel):
    """Shared scraping infrastructure settings for future extractors."""

    model_config = ConfigDict(extra="forbid")

    max_retries: int = Field(default=5, ge=0)
    retry_delay_seconds: float = Field(default=2.0, ge=0)
    checkpoint_interval: int = Field(default=100, ge=1)
    request_timeout_seconds: int = Field(default=30, ge=1)
    user_agent: str = "JMIPBot/0.1"


class ProcessingConfig(BaseModel):
    """Batch processing behavior."""

    model_config = ConfigDict(extra="forbid")

    batch_size: int = Field(default=1000, ge=1)
    strict_validation: bool = True


class CandidateProfileSettings(BaseModel):
    """Local candidate-profile settings."""

    model_config = ConfigDict(extra="forbid")

    default_profile_id: str = Field(
        default="profile-default",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    )


class ResumeIngestionConfig(BaseModel):
    """Security limits and text-quality thresholds for resume ingestion."""

    model_config = ConfigDict(extra="forbid")

    max_file_size_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    max_pdf_pages: int = Field(default=20, ge=1, le=500)
    max_docx_entries: int = Field(default=500, ge=1, le=5000)
    max_docx_uncompressed_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    max_docx_compression_ratio: float = Field(default=100.0, ge=1)
    max_extracted_characters: int = Field(default=100_000, ge=1)
    minimum_characters: int = Field(default=200, ge=1)
    minimum_words: int = Field(default=40, ge=1)
    minimum_alphanumeric_ratio: float = Field(default=0.45, ge=0, le=1)
    maximum_replacement_ratio: float = Field(default=0.01, ge=0, le=1)
    maximum_repeated_line_ratio: float = Field(default=0.2, ge=0, le=1)
    minimum_quality_score: float = Field(default=0.75, ge=0, le=1)


class SettingsConfig(BaseModel):
    """Validated contents of settings.yaml."""

    model_config = ConfigDict(extra="forbid")

    environment: str
    project: ProjectConfig
    paths: PathConfig
    storage: StorageConfig
    logging: LoggingLevelConfig
    scraping: ScrapingConfig
    processing: ProcessingConfig
    candidate_profile: CandidateProfileSettings = Field(default_factory=CandidateProfileSettings)
    resume_ingestion: ResumeIngestionConfig = Field(default_factory=ResumeIngestionConfig)


class SourceAccessPolicy(StrEnum):
    """Compliance-aware access modes for external job sources."""

    PUBLIC_API = "public_api"
    PARTNER_API = "partner_api"
    PUBLIC_FEED = "public_feed"
    MANUAL_IMPORT = "manual_import"
    UNSUPPORTED_AUTOMATION = "unsupported_automation"

    @property
    def allows_automated_collection(self) -> bool:
        """Return whether this policy can back an enabled extractor target."""
        return self in {
            SourceAccessPolicy.PUBLIC_API,
            SourceAccessPolicy.PARTNER_API,
            SourceAccessPolicy.PUBLIC_FEED,
        }


class SourceDefinition(BaseModel):
    """Configuration for an individual job source."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    type: str
    access_policy: SourceAccessPolicy
    base_url: str
    api_base_url: str | None = None
    rate_limit_per_minute: int = Field(ge=1)


class SourcesConfig(BaseModel):
    """Validated contents of sources.yaml."""

    model_config = ConfigDict(extra="forbid")

    enabled_sources: list[str] = Field(default_factory=list)
    sources: dict[str, SourceDefinition] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_enabled_sources(self) -> SourcesConfig:
        """Require enabled automation sources to exist and permit collection."""
        if len(self.enabled_sources) != len(set(self.enabled_sources)):
            raise ValueError("enabled source names must be unique")
        for name in self.enabled_sources:
            definition = self.sources.get(name)
            if definition is None:
                raise ValueError(f"enabled source is not defined: {name}")
            if not definition.enabled:
                raise ValueError(f"enabled source is marked disabled: {name}")
            if not definition.access_policy.allows_automated_collection:
                raise ValueError(
                    f"source {name} does not permit automated collection under "
                    f"access policy {definition.access_policy.value}"
                )
        return self


class AmbiguousSkillRule(BaseModel):
    """Evidence a short or common-word skill name (such as Go) must have in the text."""

    model_config = ConfigDict(extra="forbid")

    case_sensitive: bool = True
    # A mention followed by one of these is not the skill ("Go-to-market", "go-live").
    reject_followed_by: list[str] = Field(default_factory=list)
    # A mention counts when one of these appears within ``context_window`` characters
    # (other skills in the same category are always included) ...
    context_terms: list[str] = Field(default_factory=list)
    context_window: int = Field(default=60, ge=0)
    # ... or when it directly follows a lead-in ("in Go") or precedes a trailing term.
    lead_in: list[str] = Field(default_factory=list)
    trailing: list[str] = Field(default_factory=list)


class SkillsConfig(BaseModel):
    """Validated contents of skills.yaml."""

    model_config = ConfigDict(extra="forbid")

    categories: dict[str, list[str]] = Field(default_factory=dict)
    aliases: dict[str, list[str]] = Field(default_factory=dict)
    ambiguous: dict[str, AmbiguousSkillRule] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_ambiguous(self) -> SkillsConfig:
        """Require ambiguity rules to name configured skills."""
        configured = {skill for skills in self.categories.values() for skill in skills}
        unknown = sorted(set(self.ambiguous) - configured)
        if unknown:
            raise ValueError(f"ambiguous-skill rules for unconfigured skills: {unknown}")
        return self

    @model_validator(mode="after")
    def validate_aliases(self) -> SkillsConfig:
        """Require unambiguous aliases for configured canonical skills."""
        canonical_by_key = {
            skill.casefold(): skill for skills in self.categories.values() for skill in skills
        }
        seen: dict[str, str] = {}
        for canonical, aliases in self.aliases.items():
            canonical_key = canonical.casefold()
            if canonical_key not in canonical_by_key:
                raise ValueError(f"skill alias target is not configured: {canonical}")
            for alias in aliases:
                alias_key = alias.strip().casefold()
                if not alias_key:
                    raise ValueError(f"skill alias cannot be blank: {canonical}")
                owner = seen.get(alias_key)
                if owner is not None and owner != canonical_key:
                    raise ValueError(
                        f"skill alias {alias!r} maps to both {owner!r} and {canonical!r}"
                    )
                conflicting_canonical = canonical_by_key.get(alias_key)
                if conflicting_canonical is not None and alias_key != canonical_key:
                    raise ValueError(
                        f"skill alias {alias!r} conflicts with canonical skill "
                        f"{conflicting_canonical!r}"
                    )
                seen[alias_key] = canonical_key
        return self


class ResumeRelevanceWeights(BaseModel):
    """Relative contribution of each resume relevance signal."""

    model_config = ConfigDict(extra="forbid")

    skills: float = Field(default=45, ge=0)
    title: float = Field(default=30, ge=0)
    experience: float = Field(default=15, ge=0)
    location: float = Field(default=10, ge=0)

    @model_validator(mode="after")
    def validate_positive_total(self) -> ResumeRelevanceWeights:
        """Require at least one relevance signal to carry weight."""
        if self.skills + self.title + self.experience + self.location <= 0:
            raise ValueError("resume relevance weights must have a positive total")
        return self


class ResumeRelevanceScoring(BaseModel):
    """Configurable signal strengths and match-band policy."""

    model_config = ConfigDict(extra="forbid")

    core_skills: list[str] = Field(default_factory=list)
    core_skill_weight: float = Field(default=1.0, ge=0, le=1)
    supporting_skill_weight: float = Field(default=0.6, ge=0, le=1)
    explicit_skill_evidence: float = Field(default=1.0, ge=0, le=1)
    inferred_skill_evidence: float = Field(default=0.7, ge=0, le=1)
    phrase_title_signal: float = Field(default=0.9, ge=0, le=1)
    fuzzy_title_max_signal: float = Field(default=0.6, ge=0, le=1)
    seniority_signals: dict[str, float] = Field(
        default_factory=lambda: {
            "entry": 1.0,
            "mid": 1.0,
            "unknown": 0.4,
            "senior": 0.25,
            "staff": 0.05,
            "manager": 0.05,
            "director": 0.05,
            "executive": 0.05,
        }
    )
    maximum_preferred_experience_years: float = Field(default=4, ge=0)
    near_experience_signal: float = Field(default=0.5, ge=0, le=1)
    excessive_experience_signal: float = Field(default=0.1, ge=0, le=1)
    allow_relocation: bool = False
    hybrid_non_home_signal: float = Field(default=0.3, ge=0, le=1)
    unknown_location_signal: float = Field(default=0.2, ge=0, le=1)
    relocation_location_signal: float = Field(default=0.4, ge=0, le=1)
    non_home_onsite_score_cap: float = Field(default=79, ge=0, le=100)
    avoided_title_score_cap: float = Field(default=39, ge=0, le=100)
    non_preferred_employment_score_cap: float | None = Field(default=None, ge=0, le=100)
    strong_match_threshold: float = Field(default=80, ge=0, le=100)
    moderate_match_threshold: float = Field(default=60, ge=0, le=100)

    @model_validator(mode="after")
    def validate_scoring_policy(self) -> ResumeRelevanceScoring:
        """Require coherent bands and bounded seniority signal values."""
        if self.moderate_match_threshold >= self.strong_match_threshold:
            raise ValueError("moderate threshold must be below strong threshold")
        invalid = {
            name: value for name, value in self.seniority_signals.items() if value < 0 or value > 1
        }
        if invalid:
            raise ValueError("seniority signal values must be between zero and one")
        return self


class HomeBase(BaseModel):
    """A place the candidate can commute from, with nearby places and rough distances."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    max_miles: float = Field(default=40, gt=0)
    region_terms: list[str] = Field(
        default_factory=list,
        description="State names or codes; when set, one must appear too (avoids 'Wellington, NZ')",
    )
    places: dict[str, float] = Field(
        ..., min_length=1, description="Place name as it appears in postings -> approx. miles"
    )

    @field_validator("places")
    @classmethod
    def validate_places(cls, value: dict[str, float]) -> dict[str, float]:
        """Reject blank names and negative distances."""
        if any(not name.strip() or miles < 0 for name, miles in value.items()):
            raise ValueError("home base places need a name and a non-negative distance")
        return value


class TitleTier(BaseModel):
    """A priority tier of role keywords; its signal scales the title score."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    signal: float = Field(..., ge=0, le=1)
    keywords: list[str] = Field(..., min_length=1)


class ResumeProfileConfig(BaseModel):
    """Auditable, job-relevant profile used to rank canonical postings.

    Resume content (skills, experience) comes from the approved active resume when
    ``resume_source`` is ``approved_resume`` and one exists; the lists here are the
    fallback. Titles, locations, employment, and weights are preferences and stay here.
    """

    model_config = ConfigDict(extra="forbid")

    profile_name: str = Field(..., min_length=1)
    resume_source: Literal["approved_resume", "config"] = "approved_resume"
    target_titles: list[str] = Field(..., min_length=1)
    target_families: list[str] = Field(default_factory=list)
    target_seniority: list[str] = Field(default_factory=list)
    title_tiers: list[TitleTier] = Field(default_factory=list)
    avoid_title_keywords: list[str] = Field(default_factory=list)
    preferred_employment_types: list[str] = Field(default_factory=list)
    skills: list[str] = Field(..., min_length=1)
    desired_skill_matches: int = Field(default=4, ge=1)
    home_regions: list[str] = Field(default_factory=list)
    home_bases: list[HomeBase] = Field(default_factory=list)
    remote_accessible: bool = True
    weights: ResumeRelevanceWeights = Field(default_factory=ResumeRelevanceWeights)
    scoring: ResumeRelevanceScoring = Field(default_factory=ResumeRelevanceScoring)

    @model_validator(mode="after")
    def validate_core_skills(self) -> ResumeProfileConfig:
        """Require every weighted core skill to exist in the profile taxonomy."""
        skills = {skill.casefold() for skill in self.skills}
        missing = [skill for skill in self.scoring.core_skills if skill.casefold() not in skills]
        if missing:
            raise ValueError(f"core skills must be profile skills: {missing}")
        return self


class CandidateQueryConfig(BaseModel):
    """One role-focused search in a candidate collection profile."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    query: str = Field(..., min_length=1)
    location: str | None = Field(default=None, min_length=1)
    enabled: bool = True
    targets: list[str] = Field(default_factory=list)


class CandidateCollectionConfig(BaseModel):
    """Validated multi-query collection profile."""

    model_config = ConfigDict(extra="forbid")

    profile_name: str = Field(..., min_length=1)
    per_target_limit: int = Field(default=25, ge=1, le=500)
    target_names: list[str] = Field(default_factory=list)
    queries: list[CandidateQueryConfig] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_unique_query_names(self) -> CandidateCollectionConfig:
        """Require case-insensitively unique candidate query names."""
        names = [query.name.casefold() for query in self.queries]
        if len(names) != len(set(names)):
            raise ValueError("candidate query names must be unique")
        if not any(query.enabled for query in self.queries):
            raise ValueError("candidate collection requires an enabled query")
        return self


class DataQualityThresholds(BaseModel):
    """Blocking completeness and operational thresholds for the quality gate."""

    model_config = ConfigDict(extra="forbid")

    minimum_records: int = Field(default=50, ge=1)
    valid_title_coverage: float = Field(default=0.99, ge=0, le=1)
    known_location_coverage: float = Field(default=0.95, ge=0, le=1)
    description_coverage: float = Field(default=0.95, ge=0, le=1)
    source_url_coverage: float = Field(default=0.98, ge=0, le=1)
    external_id_coverage: float = Field(default=0.99, ge=0, le=1)
    skill_coverage: float = Field(default=0.50, ge=0, le=1)
    maximum_invalid_silver_rate: float = Field(default=0.01, ge=0, le=1)
    maximum_extraction_failure_rate: float = Field(default=0.05, ge=0, le=1)


class BayesianDiagnosticsConfig(BaseModel):
    """Convergence gate every sampled (MCMC) Phase 8 model must pass."""

    model_config = ConfigDict(extra="forbid")

    max_rhat: float = Field(default=1.01, ge=1.0)
    min_ess_bulk: float = Field(default=400, ge=1)
    min_ess_tail: float = Field(default=400, ge=1)
    max_divergences: int = Field(default=0, ge=0)


class BayesianQualityGateConfig(BaseModel):
    """B1: Beta-Binomial evaluation of the data-quality coverage thresholds."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    version: str = Field(default="1.0.0", min_length=1)
    prior_alpha: float = Field(default=0.5, gt=0)
    prior_beta: float = Field(default=0.5, gt=0)
    pass_probability: float = Field(default=0.9, gt=0.5, lt=1)
    fail_probability: float = Field(default=0.1, gt=0, lt=0.5)
    minimum_employer_postings: int = Field(default=3, ge=1)


class BayesianSkillSharesConfig(BaseModel):
    """B1: empirical-Bayes shrinkage of skill shares within role families."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    version: str = Field(default="1.0.0", min_length=1)
    top_skills: int = Field(default=40, ge=1)
    minimum_family_postings: int = Field(default=5, ge=1)
    fallback_prior_strength: float = Field(default=10, gt=0)
    max_prior_strength: float = Field(default=1000, gt=0)


class ComparisonWeights(BaseModel):
    """Relative weight of each application-comparison dimension."""

    model_config = ConfigDict(extra="forbid")

    requirements: float = Field(default=0.45, ge=0)
    keywords: float = Field(default=0.30, ge=0)
    seniority: float = Field(default=0.15, ge=0)
    domain: float = Field(default=0.10, ge=0)


class ComparisonConcept(BaseModel):
    """A broad ability postings name in plain words ("cloud infrastructure")."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    posting: list[str] = Field(min_length=1)
    resume: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    not_with: list[str] = Field(default_factory=list)


class ComparisonGraduationConfig(BaseModel):
    """Graduation-window lines: season names and the months each covers."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    seasons: dict[str, list[int]] = Field(default_factory=dict)
    # Words after a date that leave the window open: "December 2027 or later" ...
    later_phrases: list[str] = Field(default_factory=list)
    # ... or open before it: "May 2027 or earlier".
    earlier_phrases: list[str] = Field(default_factory=list)
    # Words before a date meaning "graduate by then": "no later than May 2027".
    by_phrases: list[str] = Field(default_factory=list)
    # A start date ("can start on September 6, 2027") is met by graduating before it.
    start_phrases: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _seasons_are_month_pairs(self) -> ComparisonGraduationConfig:
        for name, months in self.seasons.items():
            if len(months) != 2 or not all(1 <= month <= 12 for month in months):
                raise ValueError(f"season {name!r} needs [first_month, last_month] in 1-12")
        return self

    def season_windows(self) -> dict[str, tuple[int, int]]:
        """Return each season (lower-cased) as (first month, last month)."""
        return {name.casefold(): (months[0], months[1]) for name, months in self.seasons.items()}


class ComparisonBoilerplateConfig(BaseModel):
    """Posting text that is never a requirement, beyond the built-in legal and perk rules."""

    model_config = ConfigDict(extra="forbid")

    phrases: list[str] = Field(default_factory=list)
    section_breaks: list[str] = Field(default_factory=list)
    join_continuations: bool = False

    @model_validator(mode="after")
    def _patterns_compile(self) -> ComparisonBoilerplateConfig:
        for pattern in [*self.phrases, *self.section_breaks]:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid boilerplate pattern {pattern!r}: {exc}") from exc
        return self


class ComparisonLeadershipConfig(BaseModel):
    """Years lines that ask for leadership count only the resume roles that show it."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    line_patterns: list[str] = Field(default_factory=list)
    evidence_patterns: list[str] = Field(default_factory=list)
    manager_line_patterns: list[str] = Field(default_factory=list)
    manager_title_patterns: list[str] = Field(default_factory=list)
    skill_line_patterns: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _patterns_compile(self) -> ComparisonLeadershipConfig:
        for pattern in [
            *self.line_patterns,
            *self.skill_line_patterns,
            *self.evidence_patterns,
            *self.manager_line_patterns,
            *self.manager_title_patterns,
        ]:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid leadership pattern {pattern!r}: {exc}") from exc
        return self


class ComparisonLineYearsConfig(BaseModel):
    """How far short of a skill or concept line's years the resume may fall."""

    model_config = ConfigDict(extra="forbid")

    missing_below_share: float = Field(default=0.0, ge=0, le=1)


class ComparisonYearsSubjectConfig(BaseModel):
    """Years lines that name a job count only the resume roles that show it."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    role_patterns: list[str] = Field(default_factory=list)
    generic_terms: list[str] = Field(default_factory=list)
    min_shared_terms: int = Field(default=2, ge=1, le=5)

    @model_validator(mode="after")
    def _patterns_compile(self) -> ComparisonYearsSubjectConfig:
        for pattern in self.role_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid role pattern {pattern!r}: {exc}") from exc
        return self


class ComparisonDegreeFieldsConfig(BaseModel):
    """Credit for a degree at the right level in a field the resume does not show."""

    model_config = ConfigDict(extra="forbid")

    unrelated_credit: float = Field(default=0.5, ge=0, le=1)
    broad_fields: list[str] = Field(default_factory=list)
    broad_credit: float = Field(default=0.5, ge=0, le=1)
    technical_terms: list[str] = Field(default_factory=list)
    technical_groups: list[str] = Field(default_factory=list)


class ComparisonConfig(BaseModel):
    """Validated contents of comparison.yaml (Phase 6.3 application comparison)."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(default="1.0.0", min_length=1)
    weights: ComparisonWeights = Field(default_factory=ComparisonWeights)
    preferred_weight: float = Field(default=0.5, ge=0, le=1)
    text_requirement_weight: float = Field(default=0.5, ge=0, le=1)
    text_match_threshold: float = Field(default=0.6, gt=0, le=1)
    evidence_prior_weight: float = Field(default=2.0, ge=0)
    max_requirements: int = Field(default=40, ge=1)
    strong_threshold: float = Field(default=75, ge=0, le=100)
    moderate_threshold: float = Field(default=55, ge=0, le=100)
    seniority_scores: dict[str, float] = Field(
        default_factory=lambda: {
            "entry": 1.0,
            "mid": 1.0,
            "unknown": 0.8,
            "senior": 0.4,
            "staff": 0.1,
            "manager": 0.1,
            "director": 0.05,
            "executive": 0.05,
        }
    )
    domains: dict[str, list[str]] = Field(default_factory=dict)
    domain_minimum_hits: int = Field(default=2, ge=1)
    concepts: dict[str, ComparisonConcept] = Field(default_factory=dict)
    fields_of_study: dict[str, list[str]] = Field(default_factory=dict)
    traits: list[str] = Field(default_factory=list)
    synonyms: list[list[str]] = Field(default_factory=list)
    comparable_credit: float = Field(default=0.6, ge=0, le=1)
    boilerplate: ComparisonBoilerplateConfig = Field(default_factory=ComparisonBoilerplateConfig)
    years_subject: ComparisonYearsSubjectConfig = Field(
        default_factory=ComparisonYearsSubjectConfig
    )
    line_years: ComparisonLineYearsConfig = Field(default_factory=ComparisonLineYearsConfig)
    leadership: ComparisonLeadershipConfig = Field(default_factory=ComparisonLeadershipConfig)
    graduation: ComparisonGraduationConfig = Field(default_factory=ComparisonGraduationConfig)
    degree_fields: ComparisonDegreeFieldsConfig = Field(
        default_factory=ComparisonDegreeFieldsConfig
    )

    @model_validator(mode="after")
    def _concept_fields_exist(self) -> ComparisonConfig:
        for name, concept in self.concepts.items():
            unknown = set(concept.fields) - set(self.fields_of_study)
            if unknown:
                raise ValueError(f"concept {name!r} names unknown field groups: {sorted(unknown)}")
        return self


class LabelOrphanAlertConfig(BaseModel):
    """When the Labels tab warns that labels no longer match the latest comparison."""

    model_config = ConfigDict(extra="forbid")

    alert_count: int = Field(default=10, ge=1)
    alert_share: float | None = Field(default=0.1, gt=0, le=1)
    share_minimum_labels: int = Field(default=20, ge=1)


class LabelQueueConfig(BaseModel):
    """How many randomly sampled lines the review queue asks for in each group."""

    model_config = ConfigDict(extra="forbid")

    target_per_group: int = Field(default=10, ge=1, le=500)
    exclude_kinds: list[str] = Field(default_factory=lambda: ["trait"])
    seed: int = Field(default=1, ge=0)


class LabelTrackingProgressConfig(BaseModel):
    """Which tracking decisions count as preference labels, and how many each model needs."""

    model_config = ConfigDict(extra="forbid")

    positive_statuses: list[str] = Field(
        default_factory=lambda: ["reviewing", "saved", "applied", "interviewing", "offer"]
    )
    negative_statuses: list[str] = Field(default_factory=lambda: ["archived"])
    simulator_minimum: int = Field(default=10, ge=1)
    simulator_minimum_each: int = Field(default=3, ge=1)
    b3_minimum: int = Field(default=30, ge=1)
    b3_target: int = Field(default=50, ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> LabelTrackingProgressConfig:
        if self.b3_target < self.b3_minimum:
            raise ValueError("b3_target must be at least b3_minimum")
        if set(self.positive_statuses) & set(self.negative_statuses):
            raise ValueError("a status cannot be both positive and negative")
        return self


class LabelScorecardConfig(BaseModel):
    """How the Scorecard turns labels into agreement estimates (Phase 7.2)."""

    model_config = ConfigDict(extra="forbid")

    credible_level: float = Field(default=0.9, gt=0.5, lt=1)
    prior: tuple[float, float] = (0.5, 0.5)
    minimum_labels: int = Field(default=5, ge=1)
    draws: int = Field(default=20000, ge=1000, le=200000)
    seed: int = Field(default=1, ge=0)
    replay_changes_limit: int = Field(default=50, ge=0, le=1000)

    @model_validator(mode="after")
    def _positive_prior(self) -> LabelScorecardConfig:
        if min(self.prior) <= 0:
            raise ValueError("Beta prior parameters must be positive")
        return self


class LabelsConfig(BaseModel):
    """Validated contents of labels.yaml (Labels tab: review and monitor owner labels)."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(default="1.0.0", min_length=1)
    orphans: LabelOrphanAlertConfig = Field(default_factory=LabelOrphanAlertConfig)
    queue: LabelQueueConfig = Field(default_factory=LabelQueueConfig)
    tracking: LabelTrackingProgressConfig = Field(default_factory=LabelTrackingProgressConfig)
    scorecard: LabelScorecardConfig = Field(default_factory=LabelScorecardConfig)
    recent_events: int = Field(default=200, ge=10, le=5000)


class BayesianConfig(BaseModel):
    """Validated contents of bayesian.yaml (Phase 8)."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["advisory", "enforce"] = "advisory"
    credible_level: float = Field(default=0.9, gt=0.5, lt=1)
    diagnostics: BayesianDiagnosticsConfig = Field(default_factory=BayesianDiagnosticsConfig)
    quality_gate: BayesianQualityGateConfig = Field(default_factory=BayesianQualityGateConfig)
    skill_shares: BayesianSkillSharesConfig = Field(default_factory=BayesianSkillSharesConfig)


class DataQualityConfig(BaseModel):
    """Configuration for canonical and extraction quality evaluation."""

    model_config = ConfigDict(extra="forbid")

    description_minimum_characters: int = Field(default=50, ge=1)
    require_collection_summary: bool = True
    thresholds: DataQualityThresholds = Field(default_factory=DataQualityThresholds)


class LifecycleConfig(BaseModel):
    """Posting disappearance and canonical status behavior."""

    model_config = ConfigDict(extra="forbid")

    missing_runs_before_removed: int = Field(default=2, ge=1)
    update_silver_status: bool = True


class GenericYamlConfig(BaseModel):
    """Validated wrapper for extensible YAML configuration files."""

    model_config = ConfigDict(extra="allow")


class TitleFamilyConfig(BaseModel):
    """One governed family in the canonical title taxonomy."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)


class CanonicalTitleConfig(BaseModel):
    """One stable occupation-level title and its observed aliases."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str = Field(min_length=1, max_length=200)
    family: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    track: Literal["individual_contributor", "management"]
    aliases: list[str] = Field(default_factory=list)
    related_title_ids: list[str] = Field(default_factory=list)


class FamilyRuleConfig(BaseModel):
    """Keywords that place an unmatched title in a family (first matching rule wins)."""

    model_config = ConfigDict(extra="forbid")

    family: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    keywords: list[str] = Field(min_length=1)


class TitleTaxonomyConfig(BaseModel):
    """Versioned and internally consistent canonical job-title vocabulary."""

    model_config = ConfigDict(extra="forbid")

    taxonomy_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    families: dict[str, TitleFamilyConfig]
    canonical_titles: list[CanonicalTitleConfig] = Field(min_length=2)
    seniority_keywords: dict[str, list[str]]
    family_rules: list[FamilyRuleConfig] = Field(default_factory=list)
    fallback_family: str | None = None

    @model_validator(mode="after")
    def validate_taxonomy(self) -> Self:
        """Reject ambiguous aliases and dangling family or relationship references."""
        title_ids = [item.id for item in self.canonical_titles]
        if len(title_ids) != len(set(title_ids)):
            raise ValueError("canonical title IDs must be unique")
        known_ids = set(title_ids)
        known_families = set(self.families)
        surface_owner: dict[str, str] = {}
        used_families: set[str] = set()

        for item in self.canonical_titles:
            if item.family not in known_families:
                raise ValueError(
                    f"canonical title {item.id!r} references unknown family {item.family!r}"
                )
            used_families.add(item.family)
            if item.id in item.related_title_ids:
                raise ValueError(f"canonical title {item.id!r} cannot relate to itself")
            missing = set(item.related_title_ids) - known_ids
            if missing:
                raise ValueError(
                    f"canonical title {item.id!r} has unknown related IDs: {sorted(missing)}"
                )
            if len(item.related_title_ids) != len(set(item.related_title_ids)):
                raise ValueError(f"canonical title {item.id!r} repeats related title IDs")

            for surface in [item.title, *item.aliases]:
                key = " ".join(surface.casefold().split())
                if not key:
                    raise ValueError(f"canonical title {item.id!r} has an empty alias")
                owner = surface_owner.get(key)
                if owner is not None:
                    raise ValueError(
                        f"title surface {surface!r} is assigned to both {owner!r} and {item.id!r}"
                    )
                surface_owner[key] = item.id

        for rule in self.family_rules:
            if rule.family not in known_families:
                raise ValueError(f"family rule references unknown family {rule.family!r}")
            used_families.add(rule.family)
        if self.fallback_family is not None:
            if self.fallback_family not in known_families:
                raise ValueError(f"unknown fallback family {self.fallback_family!r}")
            used_families.add(self.fallback_family)
        unused = known_families - used_families
        if unused:
            raise ValueError(f"title families have no canonical titles: {sorted(unused)}")
        required_levels = {
            "entry",
            "mid",
            "senior",
            "staff",
            "manager",
            "director",
            "executive",
        }
        missing_levels = required_levels - set(self.seniority_keywords)
        if missing_levels:
            raise ValueError(f"seniority keywords omit required levels: {sorted(missing_levels)}")
        return self


class ExtractorTargetBase(BaseModel):
    """Fields shared by all configured extractor targets."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    enabled: bool = True
    company_name: str | None = Field(default=None, min_length=1)


class GreenhouseTarget(ExtractorTargetBase):
    """Configuration for one Greenhouse job board."""

    source: Literal["greenhouse"]
    board_token: str = Field(..., min_length=1)


class LeverTarget(ExtractorTargetBase):
    """Configuration for one Lever site."""

    source: Literal["lever"]
    site: str = Field(..., min_length=1)


class AshbyTarget(ExtractorTargetBase):
    """Configuration for one Ashby job board."""

    source: Literal["ashby"]
    job_board_name: str = Field(..., min_length=1)
    include_unlisted: bool = False


class WorkdayTarget(ExtractorTargetBase):
    """Configuration for one Workday tenant career site."""

    source: Literal["workday"]
    host: str = Field(..., min_length=1)
    tenant: str = Field(..., min_length=1)
    site: str = Field(..., min_length=1)
    page_size: int = Field(default=20, ge=1, le=20)


class NextEraTarget(ExtractorTargetBase):
    """Configuration for one NextEra Energy public career site."""

    source: Literal["nextera"]
    host: str = Field(..., min_length=1)
    site: str = Field(default="nextera-energy", min_length=1)


class PhenomTarget(ExtractorTargetBase):
    """Configuration for one Phenom tenant career site."""

    source: Literal["phenom"]
    host: str = Field(..., min_length=1)
    tenant: str = Field(..., min_length=1)
    site: str = Field(..., min_length=1)
    locale: str = Field(default="en_us", min_length=1)
    path_prefix: str = Field(default="/us/en", min_length=1)


ExtractorTarget = Annotated[
    GreenhouseTarget | LeverTarget | AshbyTarget | WorkdayTarget | NextEraTarget | PhenomTarget,
    Field(discriminator="source"),
]


class ExtractorTargetsConfig(BaseModel):
    """Validated collection of configured company extraction targets."""

    model_config = ConfigDict(extra="forbid")

    targets: list[ExtractorTarget] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_names(self) -> ExtractorTargetsConfig:
        """Require case-insensitively unique target names."""
        names = [target.name.casefold() for target in self.targets]
        if len(names) != len(set(names)):
            raise ValueError("extractor target names must be unique")
        return self


class Config:
    """Singleton configuration manager that lazily loads validated project settings."""

    _instance: ClassVar[Config | None] = None
    _initialized: bool

    def __new__(cls, config_dir: str | Path | None = None) -> Self:
        """Create or return the singleton configuration manager."""
        if cls._instance is None:
            instance = super().__new__(cls)
            instance._initialized = False
            cls._instance = instance
        return cast(Self, cls._instance)

    def __init__(self, config_dir: str | Path | None = None) -> None:
        """Initialize the manager without loading files until first access."""
        if self._initialized:
            return
        load_dotenv()
        self._config_dir = Path(config_dir or Path(__file__).resolve().parents[1] / "config")
        self._settings: SettingsConfig | None = None
        self._sources: SourcesConfig | None = None
        self._skills: SkillsConfig | None = None
        self._resume_profile: ResumeProfileConfig | None = None
        self._candidate_collection: CandidateCollectionConfig | None = None
        self._data_quality: DataQualityConfig | None = None
        self._bayesian: BayesianConfig | None = None
        self._comparison: ComparisonConfig | None = None
        self._labels: LabelsConfig | None = None
        self._lifecycle: LifecycleConfig | None = None
        self._companies: GenericYamlConfig | None = None
        self._salary: GenericYamlConfig | None = None
        self._locations: GenericYamlConfig | None = None
        self._titles: TitleTaxonomyConfig | None = None
        self._logging: GenericYamlConfig | None = None
        self._extractor_targets: ExtractorTargetsConfig | None = None
        self._initialized = True

    @property
    def project_root(self) -> Path:
        """Return the project directory that contains the config directory."""
        return self._config_dir.resolve().parent

    @property
    def settings(self) -> SettingsConfig:
        """Return validated project settings."""
        if self._settings is None:
            data = self._read_yaml("settings.yaml")
            data = self._apply_settings_overrides(data)
            settings = self._validate(SettingsConfig, data, "settings.yaml")
            settings.paths = settings.paths.anchored(self.project_root)
            self._settings = settings
        return self._settings

    @property
    def sources(self) -> SourcesConfig:
        """Return validated source configuration."""
        if self._sources is None:
            self._sources = self._validate(
                SourcesConfig, self._read_yaml("sources.yaml"), "sources.yaml"
            )
        return self._sources

    @property
    def skills(self) -> SkillsConfig:
        """Return validated skill taxonomy configuration."""
        if self._skills is None:
            self._skills = self._validate(
                SkillsConfig, self._read_yaml("skills.yaml"), "skills.yaml"
            )
        return self._skills

    @property
    def resume_profile(self) -> ResumeProfileConfig:
        """Return the job-relevant profile used for resume relevance scoring."""
        if self._resume_profile is None:
            filename = self.resume_profile_filename
            self._resume_profile = self._validate(
                ResumeProfileConfig, self._read_yaml(filename), filename
            )
        return self._resume_profile

    @property
    def resume_profile_filename(self) -> str:
        """Return ``resume_profile.local.yaml`` when present, else the committed template.

        The local file holds personal job-search preferences and is never committed.
        Setting ``JMIP_IGNORE_LOCAL_CONFIG=1`` (the test suite does) forces the template.
        """
        ignore = os.getenv("JMIP_IGNORE_LOCAL_CONFIG", "").strip().casefold()
        local = self._config_dir / LOCAL_RESUME_PROFILE
        if ignore not in {"1", "true", "yes"} and local.is_file():
            return LOCAL_RESUME_PROFILE
        return "resume_profile.yaml"

    @property
    def candidate_collection(self) -> CandidateCollectionConfig:
        """Return the configured role-focused candidate search profile."""
        if self._candidate_collection is None:
            self._candidate_collection = self._validate(
                CandidateCollectionConfig,
                self._read_yaml("candidate_queries.yaml"),
                "candidate_queries.yaml",
            )
        return self._candidate_collection

    @property
    def comparison(self) -> ComparisonConfig:
        """Return application-comparison settings (defaults when the file is absent)."""
        if self._comparison is None:
            path = self._config_dir / "comparison.yaml"
            data = self._read_yaml("comparison.yaml") if path.exists() else {}
            self._comparison = self._validate(ComparisonConfig, data, "comparison.yaml")
        return self._comparison

    @property
    def labels(self) -> LabelsConfig:
        """Return Labels tab settings (defaults when the file is absent)."""
        if self._labels is None:
            path = self._config_dir / "labels.yaml"
            data = self._read_yaml("labels.yaml") if path.exists() else {}
            self._labels = self._validate(LabelsConfig, data, "labels.yaml")
        return self._labels

    @property
    def bayesian(self) -> BayesianConfig:
        """Return Phase 8 Bayesian model settings (defaults when the file is absent)."""
        if self._bayesian is None:
            path = self._config_dir / "bayesian.yaml"
            data = self._read_yaml("bayesian.yaml") if path.exists() else {}
            self._bayesian = self._validate(BayesianConfig, data, "bayesian.yaml")
        return self._bayesian

    @property
    def data_quality(self) -> DataQualityConfig:
        """Return blocking Silver and collection quality thresholds."""
        if self._data_quality is None:
            self._data_quality = self._validate(
                DataQualityConfig,
                self._read_yaml("data_quality.yaml"),
                "data_quality.yaml",
            )
        return self._data_quality

    @property
    def lifecycle(self) -> LifecycleConfig:
        """Return posting lifecycle transition settings."""
        if self._lifecycle is None:
            self._lifecycle = self._validate(
                LifecycleConfig,
                self._read_yaml("lifecycle.yaml"),
                "lifecycle.yaml",
            )
        return self._lifecycle

    @property
    def salary(self) -> GenericYamlConfig:
        """Return salary normalization configuration."""
        if self._salary is None:
            self._salary = self._validate(
                GenericYamlConfig, self._read_yaml("salary.yaml"), "salary.yaml"
            )
        return self._salary

    @property
    def companies(self) -> GenericYamlConfig:
        """Return company-name normalization configuration."""
        if self._companies is None:
            self._companies = self._validate(
                GenericYamlConfig, self._read_yaml("companies.yaml"), "companies.yaml"
            )
        return self._companies

    @property
    def locations(self) -> GenericYamlConfig:
        """Return location normalization configuration."""
        if self._locations is None:
            self._locations = self._validate(
                GenericYamlConfig, self._read_yaml("locations.yaml"), "locations.yaml"
            )
        return self._locations

    @property
    def titles(self) -> TitleTaxonomyConfig:
        """Return title normalization configuration."""
        if self._titles is None:
            self._titles = self._validate(
                TitleTaxonomyConfig, self._read_yaml("titles.yaml"), "titles.yaml"
            )
        return self._titles

    @property
    def logging(self) -> GenericYamlConfig:
        """Return logging sink configuration."""
        if self._logging is None:
            self._logging = self._validate(
                GenericYamlConfig, self._read_yaml("logging.yaml"), "logging.yaml"
            )
        return self._logging

    @property
    def extractor_targets(self) -> ExtractorTargetsConfig:
        """Return configured company and career-site extraction targets."""
        if self._extractor_targets is None:
            self._extractor_targets = self._validate(
                ExtractorTargetsConfig,
                self._read_yaml("extractors.yaml"),
                "extractors.yaml",
            )
        return self._extractor_targets

    def reload(self) -> None:
        """Clear cached configuration so files are loaded again on next access."""
        self._settings = None
        self._sources = None
        self._skills = None
        self._resume_profile = None
        self._candidate_collection = None
        self._data_quality = None
        self._bayesian = None
        self._comparison = None
        self._labels = None
        self._lifecycle = None
        self._companies = None
        self._salary = None
        self._locations = None
        self._titles = None
        self._logging = None
        self._extractor_targets = None

    def _read_yaml(self, filename: str) -> dict[str, Any]:
        """Read one YAML file from the configured config directory."""
        path = self._config_dir / filename
        if not path.exists():
            raise ConfigError(f"Missing configuration file: {path}")
        try:
            with path.open("r", encoding="utf-8") as file:
                data = yaml.safe_load(file) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"Configuration file must contain a mapping: {path}")
        return data

    def _apply_settings_overrides(self, data: dict[str, Any]) -> dict[str, Any]:
        """Apply supported JMIP_* environment variable overrides to settings data."""
        overrides: dict[str, tuple[str, ...]] = {
            "JMIP_ENVIRONMENT": ("environment",),
            "JMIP_LOGGING_LEVEL": ("logging", "level"),
            "JMIP_STORAGE_DEFAULT": ("storage", "default"),
            "JMIP_SCRAPING_MAX_RETRIES": ("scraping", "max_retries"),
            "JMIP_SCRAPING_CHECKPOINT_INTERVAL": ("scraping", "checkpoint_interval"),
        }
        for env_name, path in overrides.items():
            value = os.getenv(env_name)
            if value is None:
                continue
            cursor: dict[str, Any] = data
            for key in path[:-1]:
                cursor = cursor.setdefault(key, {})
            cursor[path[-1]] = self._coerce_env_value(value)
        pipeline_dir = os.getenv("JMIP_PIPELINE_DATA_DIR", "").strip()
        if pipeline_dir:
            # Bronze, Silver, Gold, and checkpoints live in a separate data repository
            # (see .github/workflows/collect.yml); private and cache folders stay local.
            root = Path(pipeline_dir).expanduser()
            paths = data.setdefault("paths", {})
            for key, folder in PIPELINE_DATA_FOLDERS.items():
                paths[key] = str(root / folder)
        return data

    @staticmethod
    def _coerce_env_value(value: str) -> str | int | float | bool:
        """Coerce environment variable strings into common scalar types."""
        normalized = value.strip()
        if normalized.lower() in {"true", "false"}:
            return normalized.lower() == "true"
        try:
            return int(normalized)
        except ValueError:
            pass
        try:
            return float(normalized)
        except ValueError:
            return normalized

    @staticmethod
    def _validate(model: type[TConfigModel], data: dict[str, Any], filename: str) -> TConfigModel:
        """Validate configuration data with a Pydantic model."""
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise ConfigError(f"Configuration validation failed for {filename}: {exc}") from exc
