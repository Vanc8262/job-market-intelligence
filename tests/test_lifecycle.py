"""Tests for posting lifecycle transitions and canonical status updates."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from models.job_posting import (
    CompanyInfo,
    DescriptionInfo,
    JobPosting,
    PositionInfo,
    PostingMetadata,
    PostingStatus,
    SourceInfo,
    SourceType,
)
from pipeline import PostingLifecycleRunner
from storage import GoldStorage, SilverStorage
from utils.config import LifecycleConfig


def posting(at: datetime, description: str = "Build data systems with Python.") -> JobPosting:
    """Return one canonical lifecycle fixture."""
    return JobPosting(
        metadata=PostingMetadata(
            job_id="job-1",
            status=PostingStatus.ACTIVE,
            first_seen_at=at,
            last_seen_at=at,
        ),
        source=SourceInfo(
            name="lever",
            source_type=SourceType.ATS,
            external_id="external-1",
            url="https://jobs.example.com/external-1",
        ),
        company=CompanyInfo(name="Example", normalized_name="Example"),
        position=PositionInfo(title="Data Engineer", normalized_title="Data Engineer"),
        description=DescriptionInfo(clean_text=description),
        created_at=at,
        updated_at=at,
    ).with_derived_metadata()


def collection_summary(
    run_id: str,
    at: datetime,
    *,
    discovered: bool = True,
    observed: bool = True,
) -> dict[str, object]:
    """Return complete target evidence for one profile run."""
    return {
        "run_id": run_id,
        "profile_name": "fixture-profile",
        "finished_at": at.isoformat(),
        "targets": [
            {
                "target": "example-lever",
                "source": "lever",
                "succeeded": True,
                "queries": [
                    {"query_name": "data-engineering", "succeeded": True}
                ],
                "discovered_references": ["external-1"] if discovered else [],
                "observations": (
                    [
                        {
                            "reference": "external-1",
                            "job_id": "job-1",
                            "external_id": "external-1",
                        }
                    ]
                    if observed
                    else []
                ),
            }
        ],
    }


def test_lifecycle_tracks_change_missing_removal_and_reactivation(tmp_path: Path) -> None:
    t1 = datetime(2026, 8, 1, tzinfo=UTC)
    t2 = datetime(2026, 8, 2, tzinfo=UTC)
    t3 = datetime(2026, 8, 3, tzinfo=UTC)
    t4 = datetime(2026, 8, 4, tzinfo=UTC)
    t5 = datetime(2026, 8, 5, tzinfo=UTC)
    silver = SilverStorage(tmp_path / "silver")
    gold = GoldStorage(tmp_path / "gold")
    silver.save("lever/job-1", posting(t1))
    runner = PostingLifecycleRunner(
        silver_storage=silver,
        gold_storage=gold,
        lifecycle_config=LifecycleConfig(missing_runs_before_removed=2),
    )

    discovered = runner.run(collection_summary=collection_summary("run-1", t1))
    assert discovered.event_counts["discovered"] == 1
    assert discovered.current_counts == {"active": 1, "missing": 0, "removed": 0}

    replay = runner.run(collection_summary=collection_summary("run-1", t1))
    assert replay.already_applied is True
    assert replay.events == []

    changed_posting = posting(t1, "Build changed data systems with Python and SQL.")
    changed_posting.updated_at = t2
    silver.save("lever/job-1", changed_posting)
    changed = runner.run(collection_summary=collection_summary("run-2", t2))
    assert changed.event_counts["changed"] == 1
    assert silver.load("lever/job-1").metadata.last_seen_at == t2

    missing = runner.run(
        collection_summary=collection_summary(
            "run-3", t3, discovered=False, observed=False
        )
    )
    assert missing.event_counts["missing"] == 1
    assert missing.current_counts["missing"] == 1
    assert silver.load("lever/job-1").metadata.status == PostingStatus.ACTIVE

    removed = runner.run(
        collection_summary=collection_summary(
            "run-4", t4, discovered=False, observed=False
        )
    )
    assert removed.event_counts["removed"] == 1
    assert removed.current_counts["removed"] == 1
    assert silver.load("lever/job-1").metadata.status == PostingStatus.REMOVED

    reactivated = runner.run(collection_summary=collection_summary("run-5", t5))
    assert reactivated.event_counts["reactivated"] == 1
    assert reactivated.current_counts["active"] == 1
    restored = silver.load("lever/job-1")
    assert restored.metadata.status == PostingStatus.ACTIVE
    assert restored.metadata.first_seen_at == t1
    assert restored.metadata.last_seen_at == t5
    state = gold.load("lifecycle/state")
    entry = next(iter(state["entries"].values()))
    assert entry["observation_count"] == 3
    assert entry["consecutive_missing_runs"] == 0


def test_failed_target_search_does_not_age_lifecycle_state(tmp_path: Path) -> None:
    at = datetime(2026, 8, 1, tzinfo=UTC)
    silver = SilverStorage(tmp_path / "silver")
    gold = GoldStorage(tmp_path / "gold")
    silver.save("lever/job-1", posting(at))
    runner = PostingLifecycleRunner(
        silver_storage=silver,
        gold_storage=gold,
        lifecycle_config=LifecycleConfig(missing_runs_before_removed=1),
    )
    runner.run(collection_summary=collection_summary("run-1", at))
    failed = collection_summary("run-2", datetime(2026, 8, 2, tzinfo=UTC))
    failed["targets"][0]["queries"][0]["succeeded"] = False

    result = runner.run(collection_summary=failed)

    assert result.events == []
    assert result.current_counts["active"] == 1
    assert silver.load("lever/job-1").metadata.status == PostingStatus.ACTIVE
