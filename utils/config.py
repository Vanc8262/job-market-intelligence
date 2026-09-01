"""Centralized YAML configuration loader with validation and environment overrides."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Self, TypeVar, cast

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

TConfigModel = TypeVar("TConfigModel", bound=BaseModel)


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


class SourceDefinition(BaseModel):
    """Configuration for an individual job source."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    type: str
    base_url: str
    api_base_url: str | None = None
    rate_limit_per_minute: int = Field(ge=1)


class SourcesConfig(BaseModel):
    """Validated contents of sources.yaml."""

    model_config = ConfigDict(extra="forbid")

    enabled_sources: list[str] = Field(default_factory=list)
    sources: dict[str, SourceDefinition] = Field(default_factory=dict)


class SkillsConfig(BaseModel):
    """Validated contents of skills.yaml."""

    model_config = ConfigDict(extra="allow")

    categories: dict[str, list[str]] = Field(default_factory=dict)


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
    strong_match_threshold: float = Field(default=80, ge=0, le=100)
    moderate_match_threshold: float = Field(default=60, ge=0, le=100)

    @model_validator(mode="after")
    def validate_scoring_policy(self) -> ResumeRelevanceScoring:
        """Require coherent bands and bounded seniority signal values."""
        if self.moderate_match_threshold >= self.strong_match_threshold:
            raise ValueError("moderate threshold must be below strong threshold")
        invalid = {
            name: value
            for name, value in self.seniority_signals.items()
            if value < 0 or value > 1
        }
        if invalid:
            raise ValueError("seniority signal values must be between zero and one")
        return self


class ResumeProfileConfig(BaseModel):
    """Auditable, job-relevant profile used to rank canonical postings."""

    model_config = ConfigDict(extra="forbid")

    profile_name: str = Field(..., min_length=1)
    target_titles: list[str] = Field(..., min_length=1)
    target_families: list[str] = Field(default_factory=list)
    target_seniority: list[str] = Field(default_factory=list)
    skills: list[str] = Field(..., min_length=1)
    desired_skill_matches: int = Field(default=4, ge=1)
    home_regions: list[str] = Field(default_factory=list)
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
    GreenhouseTarget
    | LeverTarget
    | AshbyTarget
    | WorkdayTarget
    | NextEraTarget
    | PhenomTarget,
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
        self._lifecycle: LifecycleConfig | None = None
        self._companies: GenericYamlConfig | None = None
        self._salary: GenericYamlConfig | None = None
        self._locations: GenericYamlConfig | None = None
        self._titles: GenericYamlConfig | None = None
        self._logging: GenericYamlConfig | None = None
        self._extractor_targets: ExtractorTargetsConfig | None = None
        self._initialized = True

    @property
    def settings(self) -> SettingsConfig:
        """Return validated project settings."""
        if self._settings is None:
            data = self._read_yaml("settings.yaml")
            data = self._apply_settings_overrides(data)
            self._settings = self._validate(SettingsConfig, data, "settings.yaml")
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
            self._resume_profile = self._validate(
                ResumeProfileConfig,
                self._read_yaml("resume_profile.yaml"),
                "resume_profile.yaml",
            )
        return self._resume_profile

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
    def titles(self) -> GenericYamlConfig:
        """Return title normalization configuration."""
        if self._titles is None:
            self._titles = self._validate(
                GenericYamlConfig, self._read_yaml("titles.yaml"), "titles.yaml"
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
