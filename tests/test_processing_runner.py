"""Tests for idempotent Bronze-to-Silver processing."""

from pathlib import Path

from extractors import ExtractorRegistry
from models.job_posting import PostingStatus
from pipeline import BronzeToSilverRunner
from storage import BronzeStorage, SilverStorage
from utils.checkpoint import CheckpointManager
from utils.config import Config, ExtractorTargetsConfig


def lever_job(
    job_id: str, *, description: str = "Build APIs with Python and SQL."
) -> dict[str, object]:
    """Return a representative Lever Bronze payload."""
    return {
        "id": job_id,
        "text": "Senior Data Scientist",
        "hostedUrl": f"https://jobs.lever.co/example/{job_id}",
        "description": f"<p>{description}</p>",
        "descriptionPlain": description,
        "categories": {
            "location": "Remote",
            "department": "Data",
            "commitment": "Full-time",
        },
        "workplaceType": "remote",
    }


def build_runner(tmp_path: Path) -> tuple[BronzeToSilverRunner, BronzeStorage, SilverStorage]:
    """Build a runner with two isolated Lever routes for the same company."""
    targets = ExtractorTargetsConfig.model_validate(
        {
            "targets": [
                {
                    "name": "example-primary",
                    "source": "lever",
                    "company_name": "Example Corp",
                    "site": "example-primary",
                },
                {
                    "name": "example-secondary",
                    "source": "lever",
                    "company_name": "Example Corp",
                    "site": "example-secondary",
                },
            ]
        }
    )
    bronze = BronzeStorage(tmp_path / "bronze")
    silver = SilverStorage(tmp_path / "silver")
    registry = ExtractorRegistry(
        config=Config(),
        targets=targets,
        bronze_storage=bronze,
        checkpoint_manager=CheckpointManager(tmp_path / "checkpoints"),
    )
    return BronzeToSilverRunner(registry, silver_storage=silver), bronze, silver


def test_runner_normalizes_and_marks_cross_source_duplicates(tmp_path: Path) -> None:
    runner, bronze, silver = build_runner(tmp_path)
    bronze.save(
        "lever/example-primary/job-1/version-1",
        lever_job("job-1"),
    )
    bronze.save(
        "lever/example-secondary/job-2/version-1",
        lever_job("job-2"),
    )

    result = runner.run(run_id="processing-run-1")
    records = [
        silver.load(path.relative_to(silver.root).with_suffix("").as_posix())
        for path in silver.root.rglob("*.json")
    ]

    assert result.records_found == 2
    assert result.records_processed == 2
    assert result.silver_created == 2
    assert result.duplicates_marked == 1
    assert result.failed == 0
    assert len(records) == 2
    assert sum(record.metadata.status == PostingStatus.DUPLICATE for record in records) == 1
    active = next(record for record in records if record.metadata.status == PostingStatus.ACTIVE)
    assert active.company.normalized_name == "Example"
    assert active.position.normalized_title == "Data Scientist"
    assert active.position.seniority == "senior"
    assert active.skills.inferred == ["Python", "SQL"]
    assert active.metadata.ingestion_batch_id == "processing-run-1"


def test_runner_is_idempotent_and_updates_changed_content(tmp_path: Path) -> None:
    runner, bronze, silver = build_runner(tmp_path)
    bronze.save(
        "lever/example-primary/job-1/version-1",
        lever_job("job-1"),
    )

    first = runner.run(run_id="processing-run-1")
    second = runner.run(run_id="processing-run-2")
    bronze.save(
        "lever/example-primary/job-1/version-2",
        lever_job("job-1", description="Build production APIs with Python, SQL, and AWS."),
    )
    third = runner.run(run_id="processing-run-3")

    assert first.silver_created == 1
    assert second.silver_created == 0
    assert second.silver_updated == 0
    assert second.unchanged == 1
    assert third.silver_created == 0
    assert third.silver_updated == 1
    assert third.unchanged == 1
    assert len(list(silver.root.rglob("*.json"))) == 1


def test_runner_isolates_malformed_bronze_record(tmp_path: Path) -> None:
    runner, bronze, silver = build_runner(tmp_path)
    bronze.save(
        "lever/example-primary/good/version-1",
        lever_job("good"),
    )
    bronze.save(
        "lever/example-primary/bad/version-1",
        {"id": "bad"},
    )

    result = runner.run(target_names=["example-primary"])

    assert result.records_found == 2
    assert result.records_processed == 1
    assert result.silver_created == 1
    assert result.failed == 1
    assert result.targets["example-primary"].failed == 1
    assert result.failures[0].record.endswith("bad/version-1.json")
    assert len(list(silver.root.rglob("*.json"))) == 1


def test_runner_rejects_unknown_target_and_invalid_limit(tmp_path: Path) -> None:
    runner, _, _ = build_runner(tmp_path)

    for arguments in ({"target_names": ["missing"]}, {"limit": 0}):
        try:
            runner.run(**arguments)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid processing arguments must fail fast")


def test_runner_ignores_manual_import_snapshots_owned_by_importer(tmp_path: Path) -> None:
    runner, bronze, _ = build_runner(tmp_path)
    bronze.save(
        "manual_import/_artifacts/links/artifact-1",
        {"schema_version": 1, "content": "https://example.test/jobs/1"},
    )
    bronze.save(
        "manual_import/indeed/job-1/version-1",
        {"schema_version": 1, "record": {"url": "https://example.test/jobs/1"}},
    )

    result = runner.run()

    assert result.records_found == 0
    assert result.failed == 0


def test_runner_routes_public_discovery_bronze_scopes(tmp_path: Path) -> None:
    targets = ExtractorTargetsConfig.model_validate(
        {
            "targets": [
                {"name": "jobicy-test", "source": "jobicy"},
                {"name": "federal-test", "source": "usajobs"},
                {
                    "name": "workable-test",
                    "source": "workable",
                    "account_subdomain": "example-company",
                },
            ]
        }
    )
    bronze = BronzeStorage(tmp_path / "bronze")
    registry = ExtractorRegistry(
        config=Config(),
        targets=targets,
        bronze_storage=bronze,
        checkpoint_manager=CheckpointManager(tmp_path / "checkpoints"),
    )
    runner = BronzeToSilverRunner(
        registry,
        silver_storage=SilverStorage(tmp_path / "silver"),
    )
    bronze.save("jobicy/jobicy-test/101/version-1", {"id": "101"})
    bronze.save("usajobs/public/102/version-1", {"id": "102"})
    bronze.save("workable/example-company/103/version-1", {"id": "103"})

    snapshots, failures = runner._discover_snapshots(registry.configured_names)

    assert failures == []
    assert [(item.source_name, item.target_name) for item in snapshots] == [
        ("jobicy", "jobicy-test"),
        ("usajobs", "federal-test"),
        ("workable", "workable-test"),
    ]
