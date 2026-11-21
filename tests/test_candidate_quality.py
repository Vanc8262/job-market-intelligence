"""Tests for configurable candidate collection and the data-quality gateway."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

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
    assert checks["salary_coverage"]["verdict"] == "estimate"
    assert "minimum_records" not in checks  # a count, not a rate
    assert report["model"]["stage"] == "B1"

    gateway.bayesian = gateway.bayesian.model_copy(update={"mode": "enforce"})
    enforced = gateway.run(collection_summary=collection_summary())

    assert enforced.decision_basis == "bayesian"
    assert enforced.passed is True


def rate_limit_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[CandidateCollectionRunner, GoldStorage, httpx.Client]:
    """Build a one-target runner with no retry sleeps and a two-search 429 limit."""
    targets = ExtractorTargetsConfig.model_validate(
        {"targets": [{"name": "acme-lever", "source": "lever", "site": "acme"}]}
    )
    registry = ExtractorRegistry(
        targets=targets,
        bronze_storage=BronzeStorage(tmp_path / "bronze"),
        checkpoint_manager=CheckpointManager(tmp_path / "checkpoints"),
    )
    # Config is a shared singleton; monkeypatch restores these after the test.
    scraping = registry.config.settings.scraping
    monkeypatch.setattr(scraping, "max_retries", 0)
    monkeypatch.setattr(scraping, "rate_limited_search_limit", 2)
    monkeypatch.setattr(scraping, "rate_limited_fetch_limit", 2)
    gold = GoldStorage(tmp_path / "gold")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return CandidateCollectionRunner(registry, gold_storage=gold), gold, client


RATE_LIMIT_QUERIES = [
    "data-science",
    "machine-learning",
    "data-engineering",
    "software-engineering",
]


def test_candidate_collection_skips_a_target_that_keeps_rate_limiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the configured run of 429s, a target's remaining searches are not sent."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(429, json={"error": "Too Many Requests"})

    runner, gold, client = rate_limit_runner(tmp_path, monkeypatch, handler)
    result = runner.run(
        query_names=RATE_LIMIT_QUERIES,
        target_names=["acme-lever"],
        per_target_limit=2,
        client=client,
        run_id="rate-limit-test",
    )

    assert len(calls) == 2
    assert result.targets_failed == 1
    assert result.search_attempts == 2
    assert result.search_failures == 2
    assert result.searches_skipped == 2
    assert result.extraction_failure_rate == 1.0
    latest = gold.load("collection/latest")
    assert latest["searches_skipped"] == 2
    queries = latest["targets"][0]["queries"]
    assert [query["skipped"] for query in queries] == [False, False, True, True]
    assert "rate limited 2 searches in a row" in queries[2]["error"]


def test_candidate_collection_keeps_searching_when_a_target_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful search resets the 429 streak, so a recovering target is not skipped."""
    responses = iter([429, 200, 429, 200])
    job = lever_job("software-1", "Software Engineer I")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/acme"):
            status = next(responses)
            return httpx.Response(status, json=[job] if status == 200 else {"error": "429"})
        return httpx.Response(200, json=job)

    runner, _, client = rate_limit_runner(tmp_path, monkeypatch, handler)
    result = runner.run(
        query_names=RATE_LIMIT_QUERIES,
        target_names=["acme-lever"],
        per_target_limit=2,
        client=client,
        run_id="rate-limit-recovery-test",
    )

    assert result.targets_failed == 0
    assert result.search_attempts == 4
    assert result.search_failures == 2
    assert result.searches_skipped == 0
    assert result.postings_extracted == 1


def detail_rate_limit_handler(
    detail_statuses: list[int],
) -> tuple[Callable[[httpx.Request], httpx.Response], list[str]]:
    """Answer three searches with two matching jobs each, and detail fetches with given statuses.

    Lever clears its search cache on every search, so jobs from the first two searches are
    fetched over the network; the last search returns nothing so no job stays cached.
    """
    titles = ["Data Scientist", "Machine Learning Engineer", "Data Engineer"]
    searches = iter(
        [
            [lever_job(f"job-{index}", f"{title} {index}") for index in (2 * n + 1, 2 * n + 2)]
            for n, title in enumerate(titles)
        ]
        + [[]]
    )
    statuses = iter(detail_statuses)
    detail_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/acme"):
            return httpx.Response(200, json=next(searches))
        detail_calls.append(request.url.path.rsplit("/", 1)[-1])
        status = next(statuses)
        job_id = request.url.path.rsplit("/", 1)[-1]
        if status != 200:
            return httpx.Response(status, json={"error": "Too Many Requests"})
        return httpx.Response(200, json=lever_job(job_id, "Software Engineer"))

    return handler, detail_calls


def test_candidate_collection_skips_fetches_from_a_target_that_keeps_rate_limiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the configured run of 429 fetches, a target's remaining references are not sent."""
    handler, detail_calls = detail_rate_limit_handler([200, 429, 429])
    runner, gold, client = rate_limit_runner(tmp_path, monkeypatch, handler)
    result = runner.run(
        query_names=RATE_LIMIT_QUERIES,
        target_names=["acme-lever"],
        per_target_limit=6,
        client=client,
        run_id="fetch-rate-limit-test",
    )

    assert len(detail_calls) == 3
    assert result.references_selected == 6
    assert result.postings_extracted == 1
    assert result.posting_failures == 2
    assert result.postings_skipped == 3
    assert result.targets_failed == 0
    # 2 failed of 1 target + 4 searches + 3 fetches sent; skipped fetches were never sent.
    assert result.extraction_failure_rate == round(2 / 8, 4)
    latest = gold.load("collection/latest")
    assert latest["postings_skipped"] == 3
    assert latest["targets"][0]["postings_skipped"] == 3
    # Skipped references stay discovered, so the lifecycle does not close their postings.
    assert len(latest["targets"][0]["discovered_references"]) == 6


def test_candidate_collection_keeps_fetching_when_a_target_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful fetch or a non-429 failure resets the streak."""
    handler, detail_calls = detail_rate_limit_handler([429, 200, 429, 500, 429, 200])
    runner, _, client = rate_limit_runner(tmp_path, monkeypatch, handler)
    result = runner.run(
        query_names=RATE_LIMIT_QUERIES,
        target_names=["acme-lever"],
        per_target_limit=6,
        client=client,
        run_id="fetch-rate-limit-recovery-test",
    )

    assert len(detail_calls) == 6
    assert result.postings_extracted == 2
    assert result.posting_failures == 4
    assert result.postings_skipped == 0


def test_quality_gateway_leaves_skipped_fetches_out_of_the_failure_rate(
    tmp_path: Path,
) -> None:
    gateway, silver, _ = build_quality_gateway(tmp_path)
    silver.save("fixture/one", quality_posting("one"))
    silver.save("fixture/two", quality_posting("two"))
    summary = collection_summary()
    summary.update(references_selected=6, postings_skipped=4)

    result = gateway.run(
        collection_summary=summary,
        run_id="quality-skip-test",
        generated_at=datetime(2026, 11, 20, tzinfo=UTC),
    )

    metric = {metric.name: metric for metric in result.metrics}["extraction_failure_rate"]
    assert metric.numerator == 0
    assert metric.denominator == 1 + 2 + 6 - 4
