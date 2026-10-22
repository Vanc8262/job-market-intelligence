"""Tests for configurable candidate collection and the data-quality gateway."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from extractors import ExtractorRegistry
from models.job_posting import (
    CompanyInfo,
    DescriptionInfo,
    JobPosting,
    LocationInfo,
    PositionInfo,
    PostingMetadata,
    PostingStatus,
    SkillInfo,
    SourceInfo,
    SourceType,
    WorkplaceType,
)
from pipeline import CandidateCollectionRunner, DataQualityGateway
from storage import BronzeStorage, GoldStorage, SilverStorage
from utils.checkpoint import CheckpointManager
from utils.config import (
    DataQualityConfig,
    DataQualityThresholds,
    ExtractorTargetsConfig,
)


def lever_job(job_id: str, title: str) -> dict[str, object]:
    """Return one searchable Lever fixture."""
    return {
        "id": job_id,
        "text": title,
        "categories": {
            "location": "Remote",
            "commitment": "Full-time",
            "department": "Engineering",
        },
        "workplaceType": "remote",
        "description": f"<p>{title} builds services with Python and SQL.</p>",
        "descriptionPlain": f"{title} builds services with Python and SQL.",
        "hostedUrl": f"https://jobs.lever.co/acme/{job_id}",
    }


def test_candidate_collection_round_robins_queries_and_deduplicates_fetches(
    tmp_path: Path,
) -> None:
    jobs = [
        lever_job("data-1", "Data Engineer I"),
        lever_job("data-2", "Data Engineer II"),
        lever_job("software-1", "Software Engineer I"),
        lever_job("software-2", "Software Engineer II"),
    ]
    by_id = {str(job["id"]): job for job in jobs}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/acme"):
            return httpx.Response(200, json=jobs)
        return httpx.Response(200, json=by_id[request.url.path.rsplit("/", 1)[-1]])

    targets = ExtractorTargetsConfig.model_validate(
        {"targets": [{"name": "acme-lever", "source": "lever", "site": "acme"}]}
    )
    bronze = BronzeStorage(tmp_path / "bronze")
    gold = GoldStorage(tmp_path / "gold")
    registry = ExtractorRegistry(
        targets=targets,
        bronze_storage=bronze,
        checkpoint_manager=CheckpointManager(tmp_path / "checkpoints"),
    )
    runner = CandidateCollectionRunner(registry, gold_storage=gold)

    result = runner.run(
        query_names=["data-engineering", "software-engineering"],
        target_names=["acme-lever"],
        per_target_limit=2,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        run_id="candidate-test",
    )

    assert result.targets_failed == 0
    assert result.search_attempts == 2
    assert result.search_failures == 0
    assert result.references_discovered == 4
    assert result.references_selected == 2
    assert result.postings_extracted == 2
    assert result.unique_postings == 2
    assert result.raw_records_saved == 2
    assert result.extraction_failure_rate == 0.0
    assert list((tmp_path / "bronze" / "lever" / "acme" / "data-1").glob("*.json"))
    assert list((tmp_path / "bronze" / "lever" / "acme" / "software-1").glob("*.json"))
    assert not (tmp_path / "bronze" / "lever" / "acme" / "data-2").exists()
    latest = gold.load("collection/latest")
    assert latest["query_names"] == ["data-engineering", "software-engineering"]
    assert latest["targets"][0]["queries"][0]["new_references"] == 2
    assert latest["targets"][0]["discovered_references"] == [
        "data-1",
        "data-2",
        "software-1",
        "software-2",
    ]
    assert latest["targets"][0]["observations"] == [
        {
            "reference": "data-1",
            "job_id": result.outcomes[0].result.postings[0].metadata.job_id,
            "external_id": "data-1",
        },
        {
            "reference": "software-1",
            "job_id": result.outcomes[0].result.postings[1].metadata.job_id,
            "external_id": "software-1",
        },
    ]


def quality_posting(job_id: str) -> JobPosting:
    """Return a complete canonical posting for quality evaluation."""
    return JobPosting(
        metadata=PostingMetadata(job_id=job_id, status=PostingStatus.ACTIVE),
        source=SourceInfo(
            name="fixture",
            source_type=SourceType.ATS,
            external_id=job_id,
            url=f"https://jobs.example.com/{job_id}",
        ),
        company=CompanyInfo(name="Example", normalized_name="Example"),
        position=PositionInfo(title="Data Engineer", normalized_title="Data Engineer"),
        location=LocationInfo(
            raw="Remote - United States",
            country="United States",
            workplace_type=WorkplaceType.REMOTE,
        ),
        description=DescriptionInfo(
            clean_text="Build reliable production data pipelines with Python and SQL."
        ),
        skills=SkillInfo(inferred=["Python", "SQL"]),
    ).with_derived_metadata()


def permissive_quality(*, require_collection: bool = True) -> DataQualityConfig:
    """Return strict completeness with a small fixture record minimum."""
    return DataQualityConfig(
        description_minimum_characters=20,
        require_collection_summary=require_collection,
        thresholds=DataQualityThresholds(
            minimum_records=2,
            valid_title_coverage=1,
            known_location_coverage=1,
            description_coverage=1,
            source_url_coverage=1,
            external_id_coverage=1,
            skill_coverage=1,
            maximum_invalid_silver_rate=0,
            maximum_extraction_failure_rate=0,
        ),
    )


def collection_summary(failure_rate: float = 0.0) -> dict[str, object]:
    """Return operational evidence consumed by the quality gateway."""
    return {
        "run_id": "collection-test",
        "targets_attempted": 1,
        "targets_failed": 0,
        "search_attempts": 2,
        "search_failures": 0,
        "references_selected": 2,
        "posting_failures": 0,
        "extraction_failure_rate": failure_rate,
    }


def build_quality_gateway(
    tmp_path: Path,
) -> tuple[DataQualityGateway, SilverStorage, GoldStorage]:
    """Build an isolated gateway with fixture-sized thresholds."""
    silver = SilverStorage(tmp_path / "silver")
    gold = GoldStorage(tmp_path / "gold")
    gateway = DataQualityGateway(silver_storage=silver, gold_storage=gold)
    gateway.quality = permissive_quality()
    return gateway, silver, gold


def test_quality_gateway_passes_complete_silver_and_operational_thresholds(
    tmp_path: Path,
) -> None:
    gateway, silver, gold = build_quality_gateway(tmp_path)
    silver.save("fixture/one", quality_posting("one"))
    silver.save("fixture/two", quality_posting("two"))

    result = gateway.run(
        collection_summary=collection_summary(),
        run_id="quality-test",
        generated_at=datetime(2026, 8, 26, tzinfo=UTC),
    )

    assert result.passed is True
    assert result.failed_metrics == []
    assert result.collection_run_id == "collection-test"
    metrics = {metric.name: metric for metric in result.metrics}
    assert metrics["description_coverage"].value == 1.0
    assert metrics["extraction_failure_rate"].value == 0.0
    assert metrics["salary_coverage"].blocking is False
    assert gold.load("data_quality/latest")["passed"] is True


def test_quality_gateway_fails_when_collection_evidence_is_missing(tmp_path: Path) -> None:
    gateway, silver, _ = build_quality_gateway(tmp_path)
    silver.save("fixture/one", quality_posting("one"))
    silver.save("fixture/two", quality_posting("two"))

    result = gateway.run(generated_at=datetime(2026, 8, 26, tzinfo=UTC))

    assert result.passed is False
    assert [metric.name for metric in result.failed_metrics] == ["extraction_failure_rate"]
    extraction = result.failed_metrics[0]
    assert extraction.available is False
    assert extraction.value is None


def test_quality_gateway_reports_invalid_silver_rate(tmp_path: Path) -> None:
    gateway, silver, _ = build_quality_gateway(tmp_path)
    silver.save("fixture/one", quality_posting("one"))
    silver.save("fixture/two", quality_posting("two"))
    invalid = silver.path_for("fixture/invalid", ".json")
    invalid.parent.mkdir(parents=True, exist_ok=True)
    invalid.write_text(json.dumps({"invalid": True}), encoding="utf-8")

    result = gateway.run(collection_summary=collection_summary())

    assert result.passed is False
    assert result.invalid_records == 1
    assert "invalid_silver_rate" in [metric.name for metric in result.failed_metrics]


def test_bayesian_gate_reports_and_can_decide(tmp_path: Path) -> None:
    gateway, silver, gold = build_quality_gateway(tmp_path)
    gateway.quality = gateway.quality.model_copy(
        update={
            "thresholds": gateway.quality.thresholds.model_copy(
                update={"known_location_coverage": 0.95, "maximum_extraction_failure_rate": 0.05}
            )
        }
    )
    for index in range(20):
        posting = quality_posting(f"job-{index}")
        if index < 2:
            posting = posting.model_copy(
                update={"location": posting.location.model_copy(update={"raw": "Unknown"})}
            )
        silver.save(f"fixture/job-{index}", posting)

    advisory = gateway.run(collection_summary=collection_summary())

    assert advisory.decision_basis == "rules"
    assert "known_location_coverage" in [metric.name for metric in advisory.failed_metrics]
    report = gold.load("data_quality/latest")["bayesian"]
    checks = {check["name"]: check for check in report["checks"]}
    location = checks["known_location_coverage"]
    assert location["observed"] == 0.9
    assert location["verdict"] == "uncertain"  # 18 of 20 could still be a 95% source
    assert checks["valid_title_coverage"]["boundary_threshold"] is True
    assert report["model"]["stage"] == "B1"

    gateway.bayesian = gateway.bayesian.model_copy(update={"mode": "enforce"})
    enforced = gateway.run(collection_summary=collection_summary())

    assert enforced.decision_basis == "bayesian"
    assert enforced.passed is True
