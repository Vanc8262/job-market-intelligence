"""Tests for the local opportunities dashboard server."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import ValidationError

from dashboard.app import STATIC_DIR, create_server, load_opportunities_mart
from dashboard.tracking import ApplicationTrackingRepository


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
    server = create_server(port=0, mart_path=mart_path, tracking_path=tracking_path)
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
                    "resume_version": "analytics-v3",
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
        with urlopen(base_url) as response:  # noqa: S310
            index = response.read().decode("utf-8")
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
    assert tracking_path.exists()
    assert "Current opportunities" in index


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


def test_tracking_api_rejects_invalid_status(tmp_path: Path) -> None:
    mart_path = tmp_path / "mart.json"
    mart_path.write_text(json.dumps({"opportunities": []}), encoding="utf-8")
    server = create_server(
        port=0,
        mart_path=mart_path,
        tracking_path=tmp_path / "tracking.json",
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

    assert mart["row_count"] == len(mart["opportunities"])
    assert mart["opportunities"][0]["score_components"]["skills"]["points"] >= 0
    assert (STATIC_DIR / "styles.css").exists()
    assert "openDrawer" in (STATIC_DIR / "app.js").read_text(encoding="utf-8")
