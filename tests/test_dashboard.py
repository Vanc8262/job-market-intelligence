"""Tests for the local opportunities dashboard server."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import ValidationError

from dashboard.app import (
    MARKET_SUMMARY_PATHS,
    OPERATIONS_PATHS,
    STATIC_DIR,
    create_server,
    load_market_intelligence,
    load_opportunities_mart,
    load_pipeline_operations,
    load_resume_reviews,
)
from dashboard.tracking import ApplicationTrackingRepository
from models import (
    ExtractionStatus,
    ResumeExtraction,
    ResumeMediaType,
    ResumeReviewSection,
    ResumeVersion,
)
from profiles import CandidateProfileRepository, DefaultProfileMigrator
from utils.config import Config


def _write_market_summaries(root: Path) -> None:
    for index, relative_path in enumerate(MARKET_SUMMARY_PATHS.values()):
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"generated_at": f"2026-08-{20 + index:02d}T13:00:00+00:00"}),
            encoding="utf-8",
        )


def _write_pipeline_outputs(root: Path) -> None:
    outputs = {
        "collection": {
            "run_id": "collection-1",
            "profile_name": "test-profile",
            "query_names": ["data-science"],
            "per_target_limit": 5,
            "started_at": "2026-08-26T12:55:00+00:00",
            "finished_at": "2026-08-26T13:00:00+00:00",
            "targets_attempted": 1,
            "targets_succeeded": 1,
            "targets_failed": 0,
            "search_attempts": 1,
            "search_failures": 0,
            "references_discovered": 8,
            "references_selected": 5,
            "postings_extracted": 5,
            "unique_postings": 5,
            "raw_records_saved": 4,
            "posting_failures": 0,
            "extraction_failure_rate": 0.0,
            "targets": [
                {
                    "target": "test-greenhouse",
                    "source": "greenhouse",
                    "succeeded": True,
                    "references_discovered": 8,
                    "references_selected": 5,
                    "postings_extracted": 5,
                    "raw_records_saved": 4,
                    "posting_failures": 0,
                    "error": None,
                    "discovered_references": ["private-reference"],
                }
            ],
        },
        "quality": {
            "run_id": "quality-1",
            "collection_run_id": "collection-1",
            "generated_at": "2026-08-26T13:04:00+00:00",
            "passed": True,
            "failed_metrics": [],
            "valid_records": 5,
            "invalid_records": 0,
            "silver_records_found": 5,
            "metrics": [
                {
                    "name": "minimum_records",
                    "passed": True,
                    "blocking": True,
                    "available": True,
                    "comparison": "minimum",
                    "value": 5,
                    "threshold": 5,
                    "denominator": None,
                }
            ],
        },
        "lifecycle": {
            "run_id": "lifecycle-1",
            "collection_run_id": "collection-1",
            "observed_at": "2026-08-26T13:01:00+00:00",
            "already_applied": False,
            "tracked_postings": 5,
            "current_counts": {"active": 5, "missing": 0, "removed": 0},
            "event_counts": {"discovered": 5},
            "failures": [],
            "events": [
                {
                    "event_type": "discovered",
                    "detected_at": "2026-08-26T13:01:00+00:00",
                    "target_name": "test-greenhouse",
                    "source_name": "greenhouse",
                    "employer": "Test Company",
                    "title": "Data Scientist",
                    "previous_status": None,
                    "current_status": "active",
                    "current_content_hash": "private-hash",
                }
            ],
        },
        "lifecycle_summary": {"employer_activity": [{"employer": "Test Company", "events": 5}]},
        "manifest": {
            "run_id": "gold-1",
            "generated_at": "2026-08-26T13:03:00+00:00",
            "profile_name": "Test Candidate",
            "input_fingerprint": "fingerprint-1",
            "records_analyzed": 5,
            "silver_records_found": 5,
            "excluded_inactive": 0,
            "invalid_silver_records": 0,
            "failures": [],
            "datasets": {"skills": {"key": "skills/summary", "path": "skills/summary.json"}},
        },
    }
    for name, relative_path in OPERATIONS_PATHS.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(outputs[name]), encoding="utf-8")


def test_dashboard_serves_assets_health_and_opportunities(tmp_path: Path) -> None:
    mart_path = tmp_path / "mart.json"
    mart_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-08-26T13:00:00+00:00",
                "row_count": 1,
                "opportunities": [{"job_id": "job-1", "rank": 1}],
            }
        ),
        encoding="utf-8",
    )
    tracking_path = tmp_path / "tracking" / "state.json"
    gold_root = tmp_path / "gold"
    _write_market_summaries(gold_root)
    _write_pipeline_outputs(gold_root)
    report_mart_path = gold_root / "current_opportunities" / "mart.json"
    report_mart_path.parent.mkdir(parents=True, exist_ok=True)
    report_mart_path.write_text(mart_path.read_text(encoding="utf-8"), encoding="utf-8")
    bronze_path = tmp_path / "data" / "bronze" / "source" / "raw.json"
    bronze_path.parent.mkdir(parents=True, exist_ok=True)
    bronze_path.write_text('{"raw":true}', encoding="utf-8")
    server = create_server(
        port=0,
        mart_path=mart_path,
        tracking_path=tracking_path,
        gold_root=gold_root,
        report_root=tmp_path / "reports",
        alert_state_path=tmp_path / "alerts" / "state.json",
        operations_state_path=tmp_path / "operations" / "runs.json",
        data_project_root=tmp_path,
        profile_database=tmp_path / "profiles.sqlite3",
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base_url}/api/opportunities") as response:  # noqa: S310
            payload = json.loads(response.read())
            assert response.headers["Cache-Control"] == "no-store"
        with urlopen(f"{base_url}/api/health") as response:  # noqa: S310
            health = json.loads(response.read())
        request = Request(
            f"{base_url}/api/application-tracking/job-1",
            data=json.dumps(
                {
                    "shortlisted": True,
                    "status": "applied",
                    "priority": "high",
                    "application_date": "2026-08-26",
                    "resume_version_id": None,
                    "notes": "Follow up next week",
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        with urlopen(request) as response:  # noqa: S310
            saved = json.loads(response.read())
        with urlopen(f"{base_url}/api/application-tracking") as response:  # noqa: S310
            tracking = json.loads(response.read())
        with urlopen(f"{base_url}/api/candidate-profile") as response:  # noqa: S310
            candidate_profile = json.loads(response.read())
        with urlopen(f"{base_url}/api/resume-reviews") as response:  # noqa: S310
            resume_reviews = json.loads(response.read())
        with urlopen(f"{base_url}/api/market-intelligence") as response:  # noqa: S310
            market = json.loads(response.read())
        with urlopen(f"{base_url}/api/pipeline-operations") as response:  # noqa: S310
            operations = json.loads(response.read())
        with urlopen(f"{base_url}/api/data-management") as response:  # noqa: S310
            data_management = json.loads(response.read())
        snapshot_request = Request(f"{base_url}/api/data-snapshots", data=b"", method="POST")
        with urlopen(snapshot_request) as response:  # noqa: S310
            snapshot = json.loads(response.read())
            assert response.status == 201
        with urlopen(  # noqa: S310
            f"{base_url}/api/data-snapshots/{snapshot['filename']}"
        ) as response:
            snapshot_archive = response.read()
            snapshot_content_type = response.headers["Content-Type"]
        invalid_pipeline_run = Request(
            f"{base_url}/api/pipeline-runs",
            data=b'{"action":"arbitrary_command"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as invalid_run_error, urlopen(  # noqa: S310
            invalid_pipeline_run
        ):
            pass
        with urlopen(f"{base_url}/api/reporting") as response:  # noqa: S310
            reporting = json.loads(response.read())
        report_request = Request(
            f"{base_url}/api/reporting/generate",
            data=b"",
            method="POST",
        )
        with urlopen(report_request) as response:  # noqa: S310
            generated_report = json.loads(response.read())
            assert response.status == 201
        with urlopen(f"{base_url}/api/reporting") as response:  # noqa: S310
            reporting_after_generation = json.loads(response.read())
        with urlopen(base_url) as response:  # noqa: S310
            index = response.read().decode("utf-8")
            static_cache_control = response.headers["Cache-Control"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert payload["opportunities"][0]["job_id"] == "job-1"
    assert health == {
        "status": "ok",
        "row_count": 1,
        "generated_at": "2026-08-26T13:00:00+00:00",
    }
    assert saved["status"] == "applied"
    assert tracking["records"]["job-1"]["shortlisted"] is True
    assert tracking["schema_version"] == 2
    assert tracking["records"]["job-1"]["resume_version_id"] is None
    assert candidate_profile["profile"]["profile_id"] == "profile-default"
    assert candidate_profile["resume_versions"] == []
    assert resume_reviews["items"] == []
    assert resume_reviews["pending_count"] == 0
    assert tracking_path.exists()
    assert set(market["summaries"]) == set(MARKET_SUMMARY_PATHS)
    assert market["generated_at"] == "2026-08-26T13:04:00+00:00"
    assert operations["status"] == "stale"
    assert operations["freshness"]["collection"]["status"] == "stale"
    assert operations["control"]["can_start"] is True
    assert data_management["storage_format"] == "json"
    assert data_management["snapshot_policy"]["excludes"][0] == "data/private"
    assert snapshot["excludes_private_data"] is True
    assert snapshot_archive.startswith(b"PK")
    assert snapshot_content_type == "application/zip"
    assert invalid_run_error.value.code == 400
    assert operations["collection"]["targets"][0]["target"] == "test-greenhouse"
    assert "discovered_references" not in operations["collection"]["targets"][0]
    assert "current_content_hash" not in operations["lifecycle"]["events"][0]
    assert reporting["configured_schedule"]["cadence"] == "daily"
    assert isinstance(reporting["alerts"], list)
    assert generated_report["report_id"].startswith("report-")
    assert reporting_after_generation["report_available"] is True
    assert "Current opportunities" in index
    assert static_cache_control == "no-store"


def test_dashboard_rejects_a_second_server_on_the_same_port(tmp_path: Path) -> None:
    first = create_server(
        port=0,
        tracking_path=tmp_path / "first-tracking.json",
        profile_database=tmp_path / "first-profiles.sqlite3",
    )
    try:
        with pytest.raises(OSError):
            create_server(
                port=first.server_port,
                tracking_path=tmp_path / "second-tracking.json",
                profile_database=tmp_path / "second-profiles.sqlite3",
            )
    finally:
        first.server_close()


def test_resume_review_api_requires_all_sections_and_approves_once(tmp_path: Path) -> None:
    config = Config()
    database = tmp_path / "profiles.sqlite3"
    repository = CandidateProfileRepository(database)
    profile = DefaultProfileMigrator(repository, config).migrate().profile
    version = ResumeVersion(
        resume_version_id="resume-review-api",
        profile_id=profile.profile_id,
        version_label="Review API",
        original_filename="resume.pdf",
        storage_key=f"{profile.profile_id}/resume-review-api/original.pdf",
        media_type=ResumeMediaType.PDF,
        file_size_bytes=100,
        sha256="d" * 64,
    )
    extraction = ResumeExtraction(
        extraction_id="extraction-review-api",
        resume_version_id=version.resume_version_id,
        extractor_name="test-parser",
        extractor_version="1",
        status=ExtractionStatus.SUCCEEDED,
        document_text_sha256="e" * 64,
        character_count=800,
        extracted_at=datetime(2026, 9, 3, tzinfo=UTC),
    )
    repository.add_resume_with_extraction(version, extraction)
    projection = load_resume_reviews(repository, profile.profile_id)
    assert projection["pending_count"] == 1
    assert "text_storage_key" not in projection["items"][0]["extraction"]
    assert "document_text_sha256" not in projection["items"][0]["extraction"]
    assert projection["items"][0]["extraction"]["structured_completeness"] == {
        "score": 0.0,
        "populated_sections": 0,
        "total_sections": 4,
        "fact_count": 0,
        "section_counts": {"skills": 0, "employment": 0, "education": 0, "projects": 0},
        "approval_progress": 0.0,
    }

    mart_path = tmp_path / "mart.json"
    mart_path.write_text(
        json.dumps(
            {
                "input_fingerprint": "gold-review-api",
                "opportunities": [
                    {
                        "job_id": "job-review-api",
                        "title": "Data Engineer",
                        "skills": ["Python", "Kafka"],
                        "experience_level": "unknown",
                        "minimum_degree": "unknown",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    server = create_server(port=0, profile_database=database, mart_path=mart_path)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    incomplete = Request(
        f"{base_url}/api/resume-reviews/{extraction.extraction_id}",
        data=json.dumps({"approved_sections": ["skills"]}).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    draft = Request(
        f"{base_url}/api/resume-reviews/{extraction.extraction_id}",
        data=json.dumps(
            {
                "skills": [{"name": "Python", "confidence": 1}],
                "approved_sections": ["skills"],
                "review_comment": "Saved for later",
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    complete_payload = {
        "skills": [{"name": "Python", "confidence": 1}],
        "work_experience": [],
        "education": [],
        "projects": [],
        "approved_sections": [section.value for section in ResumeReviewSection],
        "review_comment": "Reviewed in dashboard",
    }
    complete = Request(
        f"{base_url}/api/resume-reviews/{extraction.extraction_id}",
        data=json.dumps(complete_payload).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    comparison = Request(
        f"{base_url}/api/application-comparisons/job-review-api",
        data=json.dumps({"resume_version_id": version.resume_version_id}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(draft) as response:  # noqa: S310
            saved_draft = json.loads(response.read())
        with pytest.raises(HTTPError) as incomplete_error, urlopen(incomplete):  # noqa: S310
            pass
        with urlopen(complete) as response:  # noqa: S310
            approved = json.loads(response.read())
        with urlopen(f"{base_url}/api/resume-reviews") as response:  # noqa: S310
            refreshed = json.loads(response.read())
        with urlopen(comparison) as response:  # noqa: S310
            comparison_run = json.loads(response.read())
            assert response.status == 201
        with urlopen(  # noqa: S310
            f"{base_url}/api/application-comparisons/job-review-api"
        ) as response:
            comparison_history = json.loads(response.read())
        with pytest.raises(HTTPError) as conflict_error, urlopen(complete):  # noqa: S310
            pass
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert incomplete_error.value.code == 400
    assert saved_draft["review_status"] == "pending"
    assert saved_draft["reviewed_sections"] == ["skills"]
    assert saved_draft["review_updated_at"] is not None
    assert approved["review_status"] == "approved"
    assert refreshed["approved_count"] == 1
    assert refreshed["items"][0]["extraction"]["skills"][0]["name"] == "Python"
    assert comparison_run["job_id"] == "job-review-api"
    assert comparison_run["gold_input_fingerprint"] == "gold-review-api"
    assert comparison_history["latest"]["analysis_run_id"] == comparison_run["analysis_run_id"]
    assert conflict_error.value.code == 409


def test_tracking_repository_merges_records_and_rejects_unknown_fields(tmp_path: Path) -> None:
    repository = ApplicationTrackingRepository(tmp_path / "tracking.json")

    first = repository.update("job-1", {"shortlisted": True, "priority": "medium"})
    second = repository.update("job-1", {"status": "reviewing", "notes": "Good fit"})

    assert first.shortlisted is True
    assert second.shortlisted is True
    assert second.priority.value == "medium"
    assert repository.load().records["job-1"].notes == "Good fit"
    assert not (tmp_path / "tracking.json.tmp").exists()
    with pytest.raises(ValidationError):
        repository.update("job-1", {"unknown": "field"})


def test_tracking_repository_migrates_legacy_resume_label_to_typed_id(
    tmp_path: Path,
) -> None:
    config = Config()
    profile_repository = CandidateProfileRepository(tmp_path / "profiles.sqlite3")
    profile = DefaultProfileMigrator(profile_repository, config).migrate().profile
    version = ResumeVersion(
        resume_version_id="resume-analytics-v3",
        profile_id=profile.profile_id,
        version_label="analytics-v3",
        original_filename="resume.pdf",
        storage_key=f"{profile.profile_id}/resume-analytics-v3/original.pdf",
        media_type=ResumeMediaType.PDF,
        file_size_bytes=10,
        sha256="a" * 64,
    )
    profile_repository.add_resume_version(version)
    tracking_path = tmp_path / "tracking.json"
    tracking_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "records": {
                    "job-1": {
                        "job_id": "job-1",
                        "resume_version": "analytics-v3",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    repository = ApplicationTrackingRepository(
        tracking_path,
        profile_repository=profile_repository,
        profile_id=profile.profile_id,
    )

    state = repository.load()

    assert state.schema_version == 2
    assert state.profile_id == profile.profile_id
    assert state.records["job-1"].resume_version_id == version.resume_version_id
    assert "resume_version" not in json.loads(tracking_path.read_text())["records"]["job-1"]
    with pytest.raises(ValueError, match="resume version not found"):
        repository.update("job-1", {"resume_version_id": "resume-missing"})


def test_tracking_api_rejects_invalid_status(tmp_path: Path) -> None:
    mart_path = tmp_path / "mart.json"
    mart_path.write_text(json.dumps({"opportunities": []}), encoding="utf-8")
    server = create_server(
        port=0,
        mart_path=mart_path,
        tracking_path=tmp_path / "tracking.json",
        profile_database=tmp_path / "profiles.sqlite3",
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request = Request(
        f"http://127.0.0.1:{server.server_port}/api/application-tracking/job-1",
        data=b'{"status":"not-real"}',
        method="PUT",
    )
    try:
        with pytest.raises(HTTPError) as exc_info, urlopen(request):  # noqa: S310
            pass
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert exc_info.value.code == 400


def test_dashboard_assets_and_live_mart_contract() -> None:
    mart = load_opportunities_mart()
    market = load_market_intelligence()
    operations = load_pipeline_operations()

    assert mart["row_count"] == len(mart["opportunities"])
    assert mart["opportunities"][0]["score_components"]["skills"]["points"] >= 0
    assert market["summaries"]["data_quality"]["passed"] is True
    assert operations["collection"]["targets_attempted"] >= 1
    assert (STATIC_DIR / "styles.css").exists()
    javascript = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "openDrawer" in javascript
    assert "renderMarketIntelligence" in javascript
    assert "renderPipelineOperations" in javascript
    assert "renderDataManagement" in javascript
    assert (
        "state.dataManagement = await refreshed.json();\n    renderPipelineOperations();"
        in javascript
    )
    assert "/api/data-management" in javascript
    assert "renderWeightSimulator" in javascript
    assert "calculateCalibration" in javascript
    assert "renderReporting" in javascript
    assert "/api/reporting/generate" in javascript
    assert "/api/candidate-profile" in javascript
    assert "/api/resume-reviews" in javascript
    assert 'querySelectorAll("[data-review-approved]:checked")' not in javascript
    assert "/api/application-comparisons" in javascript
    assert "renderApplicationComparison" in javascript
    assert 'name="resume_version_id"' in javascript
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'data-view="simulator"' in index
    assert 'id="simulatorPanel"' in index
    assert 'data-view="reporting"' in index
    assert 'id="reportingPanel"' in index
    assert 'data-view="resume-review"' in index
    assert 'id="resumeReviewPanel"' in index
    assert '/app.js?v=data-snapshot-v3' in index
