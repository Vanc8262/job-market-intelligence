"""Tests for the local opportunities dashboard server."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import ValidationError

from dashboard.app import (
    DEFAULT_GOLD_ROOT,
    DEFAULT_MART_PATH,
    MARKET_SUMMARY_PATHS,
    OPERATIONS_PATHS,
    STATIC_DIR,
    DashboardHTTPServer,
    create_server,
    load_market_intelligence,
    load_opportunities_mart,
    load_pipeline_operations,
)
from dashboard.tracking import ApplicationTrackingRepository


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


@contextmanager
def _running(server: DashboardHTTPServer) -> Iterator[str]:
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _write_mart(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-08-26T13:00:00+00:00",
                "row_count": 1,
                "opportunities": [{"job_id": "job-1", "rank": 1}],
            }
        ),
        encoding="utf-8",
    )


def _status(request: Request | str) -> int:
    try:
        with urlopen(request) as response:  # noqa: S310
            return int(response.status)
    except HTTPError as exc:
        return exc.code


def test_dashboard_serves_assets_health_and_opportunities(tmp_path: Path) -> None:
    mart_path = tmp_path / "mart.json"
    _write_mart(mart_path)
    tracking_path = tmp_path / "tracking" / "state.json"
    gold_root = tmp_path / "gold"
    _write_market_summaries(gold_root)
    _write_pipeline_outputs(gold_root)
    server = create_server(
        port=0,
        private_root=tmp_path / "private",
        mart_path=mart_path,
        tracking_path=tracking_path,
        gold_root=gold_root,
    )
    with _running(server) as base_url:
        with urlopen(f"{base_url}/api/opportunities") as response:  # noqa: S310
            payload = json.loads(response.read())
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["X-Frame-Options"] == "DENY"
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
        with urlopen(f"{base_url}/api/market-intelligence") as response:  # noqa: S310
            market = json.loads(response.read())
        with urlopen(f"{base_url}/api/pipeline-operations") as response:  # noqa: S310
            operations = json.loads(response.read())
        removed_endpoint = _status(f"{base_url}/api/benchmarks")
        with urlopen(base_url) as response:  # noqa: S310
            index = response.read().decode("utf-8")
            static_cache_control = response.headers["Cache-Control"]

    assert payload["opportunities"][0]["job_id"] == "job-1"
    assert health == {
        "status": "ok",
        "row_count": 1,
        "generated_at": "2026-08-26T13:00:00+00:00",
    }
    assert saved["status"] == "applied"
    assert tracking["records"]["job-1"]["shortlisted"] is True
    assert tracking["schema_version"] == 2
    assert tracking_path.exists()
    assert set(market["summaries"]) == set(MARKET_SUMMARY_PATHS)
    assert market["generated_at"] == "2026-08-26T13:04:00+00:00"
    assert operations["status"] == "stale"
    assert operations["freshness"]["collection"]["status"] == "stale"
    assert operations["collection"]["targets"][0]["target"] == "test-greenhouse"
    assert "discovered_references" not in operations["collection"]["targets"][0]
    assert "current_content_hash" not in operations["lifecycle"]["events"][0]
    assert removed_endpoint == 404
    assert "Current opportunities" in index
    assert static_cache_control == "no-store"


def test_dashboard_reports_missing_gold_data_as_unavailable(tmp_path: Path) -> None:
    server = create_server(
        port=0,
        private_root=tmp_path / "private",
        mart_path=tmp_path / "missing.json",
        tracking_path=tmp_path / "tracking.json",
        gold_root=tmp_path / "gold",
    )
    with _running(server) as base_url:
        statuses = {
            path: _status(f"{base_url}{path}")
            for path in (
                "/api/opportunities",
                "/api/health",
                "/api/market-intelligence",
                "/api/pipeline-operations",
            )
        }
        tracking_status = _status(f"{base_url}/api/application-tracking")

    assert set(statuses.values()) == {503}
    assert tracking_status == 200


def test_dashboard_blocks_dns_rebinding_and_cross_site_writes(tmp_path: Path) -> None:
    mart_path = tmp_path / "mart.json"
    _write_mart(mart_path)
    tracking_path = tmp_path / "tracking.json"
    server = create_server(
        port=0, private_root=tmp_path / "private", mart_path=mart_path, tracking_path=tracking_path
    )
    body = json.dumps({"shortlisted": True}).encode()
    with _running(server) as base_url:
        rebinding = _status(
            Request(f"{base_url}/api/opportunities", headers={"Host": "attacker.example:8765"})
        )
        localhost_name = _status(
            Request(f"{base_url}/api/opportunities", headers={"Host": "localhost:8765"})
        )
        cross_site = _status(
            Request(
                f"{base_url}/api/application-tracking/job-1",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Origin": "https://attacker.example",
                },
                method="PUT",
            )
        )
        form_post = _status(
            Request(
                f"{base_url}/api/application-tracking/job-1",
                data=b"shortlisted=true",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="PUT",
            )
        )
        same_origin = _status(
            Request(
                f"{base_url}/api/application-tracking/job-1",
                data=body,
                headers={"Content-Type": "application/json", "Origin": base_url},
                method="PUT",
            )
        )

    assert rebinding == 403
    assert localhost_name == 200
    assert cross_site == 403
    assert form_post == 415
    assert same_origin == 200
    assert json.loads(tracking_path.read_text())["records"]["job-1"]["shortlisted"] is True


def test_dashboard_rejects_a_second_server_on_the_same_port(tmp_path: Path) -> None:
    first = create_server(
        port=0, private_root=tmp_path / "private", tracking_path=tmp_path / "first-tracking.json"
    )
    try:
        with pytest.raises(OSError):
            create_server(
                port=first.server_port,
                private_root=tmp_path / "private",
                tracking_path=tmp_path / "second-tracking.json",
            )
    finally:
        first.server_close()


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


def test_tracking_repository_migrates_legacy_resume_label_into_notes(tmp_path: Path) -> None:
    tracking_path = tmp_path / "tracking.json"
    tracking_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "records": {"job-1": {"job_id": "job-1", "resume_version": "analytics-v3"}},
            }
        ),
        encoding="utf-8",
    )

    state = ApplicationTrackingRepository(tracking_path).load()

    assert state.schema_version == 2
    assert state.records["job-1"].resume_version_id is None
    assert state.records["job-1"].notes == "Legacy resume reference: analytics-v3"
    assert "resume_version" not in json.loads(tracking_path.read_text())["records"]["job-1"]


def test_tracking_api_rejects_invalid_status(tmp_path: Path) -> None:
    mart_path = tmp_path / "mart.json"
    mart_path.write_text(json.dumps({"opportunities": []}), encoding="utf-8")
    server = create_server(
        port=0,
        private_root=tmp_path / "private",
        mart_path=mart_path,
        tracking_path=tmp_path / "tracking.json",
    )
    with _running(server) as base_url:
        status = _status(
            Request(
                f"{base_url}/api/application-tracking/job-1",
                data=b'{"status":"not-real"}',
                headers={"Content-Type": "application/json"},
                method="PUT",
            )
        )

    assert status == 400


def test_dashboard_assets_reference_only_supported_features() -> None:
    javascript = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    review_script = (STATIC_DIR / "resume-review.js").read_text(encoding="utf-8")
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert (STATIC_DIR / "styles.css").exists()
    for required in (
        "openDrawer",
        "renderMarketIntelligence",
        "renderPipelineOperations",
        "renderWeightSimulator",
        "calculateCalibration",
        "/api/application-tracking",
        "resume_version_id",
    ):
        assert required in javascript
    for required in ("/api/resume-reviews", "/api/candidate-profile", "renderResumeReview"):
        assert required in review_script
    labels_script = (STATIC_DIR / "labels.js").read_text(encoding="utf-8")
    for required in (
        "/api/labels",
        "/api/labels/queue",
        "/api/labels/changes",
        "/api/labels/snapshots",
        "renderChanges",
        "/api/requirement-labels",
        "renderLabels",
        "renderQueue",
        "orphan_alert",
    ):
        assert required in labels_script
    for removed in (
        "/api/benchmarks",
        "/api/model-evaluations",
        "/api/reporting",
        "/api/application-comparisons",
        "/api/data-management",
        "/api/pipeline-runs",
    ):
        assert removed not in javascript + review_script + labels_script
    for view in (
        "opportunities",
        "shortlist",
        "intelligence",
        "operations",
        "simulator",
        "resume-review",
        "labels",
    ):
        assert f'data-view="{view}"' in index
    assert 'data-view="benchmarks"' not in index
    assert "/app.js?v=resume-v17" in index
    assert "/resume-review.js?v=resume-v17" in index
    assert "/labels.js?v=resume-v17" in index
    assert "/styles.css?v=resume-v17" in index


@pytest.mark.skipif(
    not DEFAULT_MART_PATH.exists() or not (DEFAULT_GOLD_ROOT / "manifest/latest.json").exists(),
    reason="requires locally built Gold data (run the pipeline first)",
)
def test_live_gold_outputs_match_the_dashboard_contract() -> None:
    mart = load_opportunities_mart()
    market = load_market_intelligence()
    operations = load_pipeline_operations()

    assert mart["row_count"] == len(mart["opportunities"])
    assert set(market["summaries"]) == set(MARKET_SUMMARY_PATHS)
    assert operations["collection"]["targets_attempted"] >= 1
