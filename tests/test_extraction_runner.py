"""Tests for extraction orchestration and Bronze preservation."""

import json
from pathlib import Path

import httpx

from extractors import ExtractorRegistry
from pipeline import ExtractionRunner
from storage import BronzeStorage
from utils.checkpoint import CheckpointManager
from utils.config import ExtractorTargetsConfig


def lever_payload() -> dict:
    """Return a representative Lever posting payload."""
    return {
        "id": "lever-201",
        "text": "Data Engineer",
        "categories": {
            "location": "Remote",
            "commitment": "Full-time",
            "department": "Engineering",
        },
        "workplaceType": "remote",
        "description": "<p>Build data pipelines with Python.</p>",
        "descriptionPlain": "Build data pipelines with Python.",
        "hostedUrl": "https://jobs.lever.co/acme/lever-201",
    }


def ashby_payload() -> dict:
    """Return a representative Ashby posting payload."""
    return {
        "id": "ashby-201",
        "title": "Analytics Engineer",
        "location": "New York, NY",
        "department": "Data",
        "isListed": True,
        "workplaceType": "Hybrid",
        "descriptionPlain": "Develop analytics models with SQL.",
        "publishedAt": "2026-08-20T12:00:00Z",
        "employmentType": "FullTime",
        "jobUrl": "https://jobs.ashbyhq.com/acme/ashby-201",
    }


def build_registry(tmp_path: Path, targets: list[dict]) -> ExtractorRegistry:
    """Build an isolated registry for extraction runner tests."""
    return ExtractorRegistry(
        targets=ExtractorTargetsConfig.model_validate({"targets": targets}),
        bronze_storage=BronzeStorage(tmp_path / "bronze"),
        checkpoint_manager=CheckpointManager(tmp_path / "checkpoints"),
    )


def test_runner_extracts_and_preserves_raw_payload_in_bronze(tmp_path: Path) -> None:
    registry = build_registry(
        tmp_path,
        [{"name": "acme-lever", "source": "lever", "site": "acme"}],
    )
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=[lever_payload()])
        )
    )
    runner = ExtractionRunner(registry)

    batch = runner.run("data engineer", target_names=["acme-lever"], client=client)
    raw_files = list((tmp_path / "bronze" / "lever" / "acme" / "lever-201").glob("*.json"))

    assert batch.targets_succeeded == 1
    assert batch.targets_failed == 0
    assert batch.postings_extracted == 1
    assert batch.raw_records_saved == 1
    assert len(raw_files) == 1
    assert json.loads(raw_files[0].read_text(encoding="utf-8")) == lever_payload()


def test_runner_is_idempotent_for_unchanged_raw_payloads(tmp_path: Path) -> None:
    registry = build_registry(
        tmp_path,
        [{"name": "acme-lever", "source": "lever", "site": "acme"}],
    )
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=[lever_payload()])
        )
    )
    runner = ExtractionRunner(registry)

    first = runner.run("data", client=client)
    second = runner.run("data", client=client)

    assert first.raw_records_saved == 1
    assert second.raw_records_saved == 0
    assert second.postings_extracted == 1
    assert len(list((tmp_path / "bronze").rglob("*.json"))) == 1


def test_runner_isolates_target_failure_and_continues(tmp_path: Path) -> None:
    registry = build_registry(
        tmp_path,
        [
            {"name": "broken-lever", "source": "lever", "site": "broken"},
            {"name": "acme-ashby", "source": "ashby", "job_board_name": "acme"},
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "lever.co" in request.url.host:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json={"apiVersion": "1", "jobs": [ashby_payload()]})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    batch = ExtractionRunner(registry).run("analytics", client=client)

    assert batch.targets_succeeded == 1
    assert batch.targets_failed == 1
    assert batch.postings_extracted == 1
    assert batch.raw_records_saved == 1
    assert batch.outcomes[0].error is not None
    assert batch.outcomes[1].result is not None
    assert list((tmp_path / "bronze" / "ashby" / "acme" / "ashby-201").glob("*.json"))


def test_runner_with_no_enabled_targets_returns_empty_success(tmp_path: Path) -> None:
    registry = build_registry(
        tmp_path,
        [
            {
                "name": "disabled-lever",
                "source": "lever",
                "site": "acme",
                "enabled": False,
            }
        ],
    )

    batch = ExtractionRunner(registry).run("")

    assert batch.outcomes == []
    assert batch.targets_succeeded == 0
    assert batch.targets_failed == 0
    assert batch.raw_records_saved == 0
    assert batch.summary()["targets_attempted"] == 0
