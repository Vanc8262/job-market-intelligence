"""Tests for Phase 1 foundation infrastructure."""

from io import StringIO
from pathlib import Path
from typing import Any

from extractors import BaseExtractor
from models.job_posting import (
    CompanyInfo,
    ExperienceInfo,
    ExperienceLevel,
    JobPosting,
    PositionInfo,
    PostingMetadata,
    PostingStatus,
    SalaryInfo,
    SourceInfo,
    SourceType,
)
from storage import BronzeStorage, GoldStorage, SilverStorage
from storage.base import BaseStorage
from utils.checkpoint import Checkpoint, CheckpointManager
from utils.config import Config
from utils.hashing import content_hash, duplicate_detection_hash, generate_job_id
from utils.logger import bind_context, log_event, logger


class MemoryStorage(BaseStorage):
    """In-memory storage backend for extractor contract tests."""

    def __init__(self) -> None:
        self.root = Path()
        self.records: dict[str, Any] = {}

    def save(self, key: str, payload: Any) -> Path | None:
        self.records[key] = payload
        return Path(key)

    def load(self, key: str) -> Any:
        return self.records[key]

    def exists(self, key: str) -> bool:
        return key in self.records

    def delete(self, key: str) -> None:
        self.records.pop(key, None)


class FakeExtractor(BaseExtractor):
    """Minimal concrete extractor used to validate the shared interface."""

    def search_jobs(self, query: str, location: str | None = None, **filters: object) -> list[str]:
        assert query == "data scientist"
        assert location == "Remote"
        assert filters == {"seniority": "senior"}
        return ["job-1", "job-2"]

    def fetch_job(self, job_reference: str) -> dict[str, Any]:
        return {
            "id": job_reference,
            "company": "Example Corp",
            "title": "Senior Data Scientist",
            "location": "Remote",
        }

    def parse_job(self, raw_job: dict[str, Any]) -> JobPosting:
        return JobPosting(
            metadata=PostingMetadata(job_id=raw_job["id"]),
            source=SourceInfo(
                name=self.source_name,
                source_type=SourceType.OTHER,
                external_id=raw_job["id"],
            ),
            company=CompanyInfo(name=raw_job["company"]),
            position=PositionInfo(title=raw_job["title"]),
        )


def test_config_loads_settings_and_sources() -> None:
    config = Config()

    assert config.settings.scraping.max_retries == 5
    assert "greenhouse" in config.sources.enabled_sources
    assert "programming_languages" in config.skills.categories
    assert "Data Scientist" in config.resume_profile.target_titles
    assert config.resume_profile.weights.model_dump() == {
        "skills": 35.0,
        "title": 30.0,
        "experience": 20.0,
        "location": 15.0,
    }
    assert config.resume_profile.scoring.allow_relocation is False
    assert config.resume_profile.scoring.strong_match_threshold == 80
    assert config.candidate_collection.per_target_limit == 25
    assert len(config.candidate_collection.queries) == 6
    assert config.data_quality.thresholds.valid_title_coverage == 0.99


def test_job_posting_model_validates_minimal_record() -> None:
    posting = JobPosting(
        metadata=PostingMetadata(job_id="job-123"),
        source=SourceInfo(name="greenhouse", source_type=SourceType.ATS),
        company=CompanyInfo(name="Example Corp"),
        position=PositionInfo(title="Data Scientist"),
    )

    assert posting.company.name == "Example Corp"
    assert posting.position.title == "Data Scientist"
    assert posting.source.source_type == SourceType.ATS


def test_job_posting_model_refines_status_compensation_and_experience() -> None:
    posting = JobPosting(
        metadata=PostingMetadata(job_id="", status=PostingStatus.ACTIVE),
        source=SourceInfo(name="greenhouse", source_type=SourceType.ATS, external_id="123"),
        company=CompanyInfo(name="Example Corp"),
        position=PositionInfo(title="Senior Data Scientist"),
        salary=SalaryInfo(min_amount=120000, max_amount=150000, currency="usd"),
        experience=ExperienceInfo(level=ExperienceLevel.SENIOR, min_years=5, max_years=8),
    ).with_derived_metadata()

    assert posting.metadata.is_active is True
    assert posting.salary.currency == "USD"
    assert posting.metadata.job_id
    assert posting.metadata.content_hash
    assert posting.metadata.duplicate_hash


def test_checkpoint_round_trip(tmp_path: Path) -> None:
    manager = CheckpointManager(tmp_path)
    checkpoint = Checkpoint(name="greenhouse-search", position=10, state={"page": 2})

    manager.save(checkpoint)
    loaded = manager.load("greenhouse-search")

    assert loaded is not None
    assert loaded.position == 10
    assert loaded.state == {"page": 2}


def test_hashes_are_deterministic() -> None:
    first = generate_job_id("greenhouse", "Example", "ML Engineer", "Remote", "123")
    second = generate_job_id("greenhouse", "Example", "ML Engineer", "Remote", "123")

    assert first == second
    assert content_hash("Build models", "ML Engineer", "Example")
    assert duplicate_detection_hash("Example", "ML Engineer", "Remote")


def test_base_extractor_runs_shared_extraction_workflow(tmp_path: Path) -> None:
    storage = MemoryStorage()
    checkpoints = CheckpointManager(tmp_path)
    extractor = FakeExtractor(
        "greenhouse",
        bronze_storage=storage,
        checkpoint_manager=checkpoints,
    )

    result = extractor.extract("data scientist", "Remote", seniority="senior")

    assert result.source_name == "greenhouse"
    assert result.references_found == 2
    assert result.succeeded == 2
    assert result.failed == 0
    assert result.raw_records_saved == 2
    assert list(storage.records) == ["greenhouse/job-1", "greenhouse/job-2"]
    assert [posting.metadata.job_id for posting in result.postings] == ["job-1", "job-2"]

    checkpoint = extractor.load_checkpoint()
    assert checkpoint is not None
    assert checkpoint.position == 2
    assert checkpoint.state["succeeded"] == 2


def test_bronze_storage_preserves_raw_records_immutably(tmp_path: Path) -> None:
    storage = BronzeStorage(tmp_path)
    payload = {"id": "job-1", "nested": {"title": "Data Scientist"}}

    path = storage.save("greenhouse/job-1", payload)

    assert path == tmp_path.resolve() / "greenhouse" / "job-1.json"
    assert storage.exists("greenhouse/job-1")
    assert storage.load("greenhouse/job-1") == payload

    try:
        storage.save("greenhouse/job-1", {"id": "job-1", "changed": True})
    except FileExistsError:
        pass
    else:
        raise AssertionError("bronze storage must not overwrite raw records")


def test_silver_storage_validates_canonical_postings(tmp_path: Path) -> None:
    storage = SilverStorage(tmp_path)
    posting = JobPosting(
        metadata=PostingMetadata(job_id="job-123"),
        source=SourceInfo(name="greenhouse", source_type=SourceType.ATS),
        company=CompanyInfo(name="Example Corp"),
        position=PositionInfo(title="Data Scientist"),
    )

    storage.save(posting.metadata.job_id, posting)
    loaded = storage.load(posting.metadata.job_id)

    assert loaded == posting
    assert loaded.source.name == "greenhouse"


def test_gold_storage_round_trips_analytics_payloads(tmp_path: Path) -> None:
    storage = GoldStorage(tmp_path)
    payload = [
        {"month": "2026-07", "skill": "Python", "posting_count": 10},
        {"month": "2026-07", "skill": "SQL", "posting_count": 8},
    ]

    storage.save("skills/monthly_counts", payload)

    assert storage.exists("skills/monthly_counts")
    assert storage.load("skills/monthly_counts") == payload


def test_storage_rejects_unsafe_keys(tmp_path: Path) -> None:
    storage = GoldStorage(tmp_path)

    try:
        storage.save("../outside", {"bad": True})
    except ValueError:
        pass
    else:
        raise AssertionError("storage keys must not escape the storage root")


def test_logger_binds_standard_context_fields() -> None:
    sink = StringIO()
    logger.remove()
    logger.add(
        sink,
        format="{extra[component]}|{extra[source]}|{extra[job_id]}|{extra[event]}|{message}",
    )

    bind_context(component="extractor", source="greenhouse", job_id="job-1").info("Fetched")
    log_event("INFO", "Saved", component="storage", event="silver_save", source="greenhouse")
    log_event("INFO", "Saved {count} records", component="storage", count=2)

    output = sink.getvalue()
    assert "extractor|greenhouse|job-1|-|Fetched" in output
    assert "storage|greenhouse|-|silver_save|Saved" in output
    assert "storage|-|-|-|Saved 2 records" in output
    logger.remove()
