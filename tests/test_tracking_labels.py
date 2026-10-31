"""Tests for B3 training labels: the tracking-decision log, review, sync, and snapshots."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen

import pytest

from analytics.tracking_labels import (
    DecisionContext,
    TrackingClass,
    TrackingLabelStore,
    TrackingSource,
    create_tracking_snapshot,
    decision_context,
    list_tracking_snapshots,
    record_change,
    review_tracking,
    summarize_tracking,
    sync,
    tracking_class,
    tracking_labels_path,
    tracking_snapshot_labels,
    training_rows,
    unlogged,
)
from comparison.runner import results_path
from dashboard.app import DashboardHTTPServer, create_server
from scripts import tracking_labels as tracking_cli
from utils.config import LabelsConfig

START = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
SETTINGS = LabelsConfig()


def _row(job_id: str, score: float = 72.5) -> dict[str, object]:
    return {
        "job_id": job_id,
        "title": f"Data Analyst {job_id}",
        "employer": "Example Health",
        "family": "data_analytics",
        "seniority": "entry",
        "workplace_type": "remote",
        "resume_relevance_score": score,
        "pre_cap_score": score,
        "match_band": "moderate",
        "score_cap_reason": None,
        "title_tier": "core",
        "family_match": True,
        "home_region_match": False,
        "employment_preferred": True,
        "minimum_experience_years": 2,
        "salary_midpoint": 85000.0,
        "skill_match_details": [{"skill": "python"}],
        "score_components": {
            "skills": {"signal": 0.8, "points": 32.0},
            "title": {"signal": 1.0, "points": 25.0},
            "experience": {"signal": 0.5, "points": 10.0},
            "location": {"signal": 0.0, "points": 0.0},
        },
    }


def _mart(*job_ids: str) -> dict[str, object]:
    return {
        "generated_at": "2026-10-30T10:00:00+00:00",
        "row_count": len(job_ids),
        "profile": {"weights": {"skills": 40, "title": 25, "experience": 20, "location": 15}},
        "opportunities": [_row(job_id) for job_id in job_ids],
    }


def _comparisons() -> dict[str, object]:
    return {
        "model": {"version": "1.5.0"},
        "resume": {"resume_version_id": "resume-v3"},
        "jobs": {
            "job-1": {
                "fit_score": 64.0,
                "band": "moderate",
                "dimensions": {
                    "requirements": {"score": 0.6, "required_met": 3, "required_total": 5},
                    "keywords": {"score": 0.7},
                },
            }
        },
    }


def _context(*job_ids: str) -> DecisionContext:
    return decision_context(_mart(*job_ids), _comparisons())


def test_a_decision_is_logged_with_the_signals_shown_at_the_time(tmp_path: Path) -> None:
    store = TrackingLabelStore(tracking_labels_path(tmp_path))

    label = store.record("job-1", "applied", _context("job-1"), SETTINGS, now=START)

    assert label.label_class is TrackingClass.POSITIVE
    assert label.captured is True
    assert label.features["resume_relevance_score"] == 72.5
    assert label.features["skills_signal"] == 0.8
    assert label.features["location_signal"] == 0.0
    assert label.features["fit_score"] == 64.0
    assert label.features["requirements_score"] == 0.6
    # Lists and nested evidence are not features.
    assert "skill_match_details" not in label.features
    assert label.posting["title"] == "Data Analyst job-1"
    assert label.relevance_weights == {
        "skills": 40.0,
        "title": 25.0,
        "experience": 20.0,
        "location": 15.0,
    }
    assert (label.comparison_model_version, label.resume_version_id) == ("1.5.0", "resume-v3")
    assert store.all() == [label]


def test_statuses_map_to_training_classes() -> None:
    assert tracking_class("saved", SETTINGS) is TrackingClass.POSITIVE
    assert tracking_class("archived", SETTINGS) is TrackingClass.NEGATIVE
    # A rejection is an outcome, not a preference.
    assert tracking_class("rejected", SETTINGS) is TrackingClass.NEITHER


def test_only_status_changes_are_logged(tmp_path: Path) -> None:
    store = TrackingLabelStore(tracking_labels_path(tmp_path))
    context = _context("job-1")

    assert record_change(store, "job-1", None, "new", context, SETTINGS) is None
    assert record_change(store, "job-1", "saved", "saved", context, SETTINGS) is None
    saved = record_change(store, "job-1", None, "saved", context, SETTINGS)
    archived = record_change(store, "job-1", "saved", "archived", context, SETTINGS)

    assert saved is not None and archived is not None
    assert [label.status for label in store.all()] == ["saved", "archived"]
    assert store.latest()["job-1"].label_class is TrackingClass.NEGATIVE


def test_withdraw_and_restore_keep_the_original_signals(tmp_path: Path) -> None:
    store = TrackingLabelStore(tracking_labels_path(tmp_path))
    first = store.record("job-1", "archived", _context("job-1"), SETTINGS, now=START)

    withdrawn = store.withdraw("job-1", note="  archived as a duplicate ", now=START)
    with pytest.raises(LookupError):
        store.withdraw("job-1")
    restored = store.restore("job-1", now=START + timedelta(minutes=1))
    with pytest.raises(LookupError):
        store.restore("job-1")

    assert withdrawn.withdrawn and withdrawn.note == "archived as a duplicate"
    assert not restored.withdrawn
    assert restored.source is TrackingSource.LABELS_TAB
    assert restored.features == first.features
    assert restored.status == "archived"
    with pytest.raises(LookupError):
        store.withdraw("job-2")


def test_sync_adds_decisions_the_log_lacks_once(tmp_path: Path) -> None:
    store = TrackingLabelStore(tracking_labels_path(tmp_path))
    store.record("job-1", "saved", _context("job-1"), SETTINGS, now=START)
    statuses = {"job-1": "applied", "job-2": "archived", "job-3": "new"}

    assert unlogged(store.all(), statuses) == {"job-1": "applied", "job-2": "archived"}
    added = sync(store, statuses, _context("job-1"), SETTINGS, now=START)

    assert [(label.job_id, label.source) for label in added] == [
        ("job-1", TrackingSource.SYNC),
        ("job-2", TrackingSource.SYNC),
    ]
    # job-2 left the mart before the sync: logged, but without signals.
    assert added[1].captured is False and added[1].features == {}
    assert sync(store, statuses, _context(), SETTINGS) == []


def test_review_rows_and_summary(tmp_path: Path) -> None:
    store = TrackingLabelStore(tracking_labels_path(tmp_path))
    context = _context("job-1", "job-2", "job-3")
    store.record("job-1", "applied", context, SETTINGS, now=START)
    store.record("job-2", "archived", context, SETTINGS, now=START + timedelta(minutes=1))
    store.record("job-3", "rejected", context, SETTINGS, now=START + timedelta(minutes=2))
    store.record("job-4", "saved", context, SETTINGS, now=START + timedelta(minutes=3))
    store.withdraw("job-2", now=START + timedelta(minutes=4))
    statuses = {"job-1": "interviewing", "job-2": "archived", "job-3": "rejected"}

    rows = review_tracking(store.all(), statuses, context.postings, SETTINGS)
    by_job = {row["job_id"]: row for row in rows}
    summary = summarize_tracking(rows, unlogged(store.all(), statuses), SETTINGS)

    assert [row["job_id"] for row in rows][0] == "job-2"  # newest entry first
    assert by_job["job-1"]["in_sync"] is False
    assert by_job["job-1"]["tracking_status"] == "interviewing"
    assert by_job["job-2"]["withdrawn"] is True
    assert len(by_job["job-2"]["history"]) == 2  # type: ignore[arg-type]
    assert by_job["job-4"]["state"] == "closed"
    assert by_job["job-4"]["title"] is None  # never in the mart, so nothing captured
    assert summary["positive"] == 2 and summary["negative"] == 0
    assert summary["neither"] == 1 and summary["withdrawn"] == 1
    assert summary["unlogged"] == 1 and summary["closed"] == 1
    assert summary["progress"]["total"] == 2  # type: ignore[index]


def test_training_rows_keep_only_active_preferences(tmp_path: Path) -> None:
    store = TrackingLabelStore(tracking_labels_path(tmp_path))
    context = _context("job-1", "job-2", "job-3")
    store.record("job-1", "saved", context, SETTINGS, now=START)
    store.record("job-2", "archived", context, SETTINGS, now=START)
    store.record("job-3", "rejected", context, SETTINGS, now=START)
    store.record("job-4", "archived", context, SETTINGS, now=START)
    store.withdraw("job-4")

    rows = {row["job_id"]: row for row in training_rows(store.all(), SETTINGS)}

    assert set(rows) == {"job-1", "job-2"}
    assert (rows["job-1"]["label"], rows["job-2"]["label"]) == (1, 0)
    assert rows["job-1"]["skills_signal"] == 0.8
    # The status lists in labels.yaml decide the class when the log is read.
    strict = LabelsConfig.model_validate(
        {"tracking": {"negative_statuses": ["archived", "rejected"]}}
    )
    reread = {row["job_id"]: row["label"] for row in training_rows(store.all(), strict)}
    assert reread == {"job-1": 1, "job-2": 0, "job-3": 0}


def test_snapshots_are_idempotent_verified_and_reproducible(tmp_path: Path) -> None:
    store = TrackingLabelStore(tracking_labels_path(tmp_path))
    store.record("job-1", "saved", _context("job-1"), SETTINGS, now=START)
    store.record("job-2", "archived", _context("job-2"), SETTINGS, now=START)

    manifest, created = create_tracking_snapshot(store, SETTINGS, now=START)
    again, created_again = create_tracking_snapshot(store, SETTINGS)
    store.record("job-3", "applied", _context(), SETTINGS)

    assert created and not created_again
    assert again["snapshot_id"] == manifest["snapshot_id"]
    assert (manifest["positive"], manifest["negative"]) == (1, 1)
    assert [label.job_id for label in tracking_snapshot_labels(store, manifest)] == [
        "job-1",
        "job-2",
    ]
    assert [item["verified"] for item in list_tracking_snapshots(store)] == [True]

    store.path.write_text(store.path.read_text().replace("saved", "applied"), encoding="utf-8")
    assert [item["verified"] for item in list_tracking_snapshots(store)] == [False]
    with pytest.raises(ValueError, match="no longer matches"):
        tracking_snapshot_labels(store, manifest)


def test_a_torn_final_line_is_skipped(tmp_path: Path) -> None:
    store = TrackingLabelStore(tracking_labels_path(tmp_path))
    store.record("job-1", "saved", _context("job-1"), SETTINGS, now=START)
    with store.path.open("a", encoding="utf-8") as stream:
        stream.write('{"schema_version": 1, "job_')

    assert [label.job_id for label in store.all()] == ["job-1"]


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


def _send(url: str, payload: dict[str, object], method: str = "POST") -> tuple[int, object]:
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urlopen(request) as response:  # noqa: S310
        return int(response.status), json.loads(response.read())


def _get(url: str) -> dict[str, object]:
    with urlopen(url) as response:  # noqa: S310
        return dict(json.loads(response.read()))


def _server(tmp_path: Path, tracking: Path) -> DashboardHTTPServer:
    private_root = tmp_path / "private"
    results = results_path(private_root)
    results.parent.mkdir(parents=True)
    results.write_text(json.dumps(_comparisons()), encoding="utf-8")
    mart_path = tmp_path / "mart.json"
    mart_path.write_text(json.dumps(_mart("job-1", "job-2")), encoding="utf-8")
    return create_server(
        port=0, private_root=private_root, tracking_path=tracking, mart_path=mart_path
    )


def test_dashboard_logs_status_changes_and_serves_training_data(tmp_path: Path) -> None:
    tracking = tmp_path / "tracking.json"
    server = _server(tmp_path, tracking)
    with _running(server) as base:
        _send(f"{base}/api/application-tracking/job-1", {"status": "saved"}, "PUT")
        _send(f"{base}/api/application-tracking/job-1", {"notes": "call recruiter"}, "PUT")
        _send(f"{base}/api/application-tracking/job-1", {"status": "applied"}, "PUT")
        _send(f"{base}/api/application-tracking/job-2", {"shortlisted": True}, "PUT")
        data = _get(f"{base}/api/labels/tracking")
        withdraw_status, _ = _send(
            f"{base}/api/labels/tracking", {"action": "withdraw", "job_id": "job-1"}
        )
        after = _get(f"{base}/api/labels/tracking")
        snapshot_status, snapshot = _send(f"{base}/api/labels/tracking/snapshots", {})
        changes = _get(f"{base}/api/labels/changes")

    rows = data["rows"]
    assert isinstance(rows, list) and len(rows) == 1  # job-2 is still "new"
    assert rows[0]["status"] == "applied"
    assert [entry["status"] for entry in rows[0]["history"]] == ["saved", "applied"]
    assert rows[0]["features"]["fit_score"] == 64.0
    assert data["summary"]["positive"] == 1  # type: ignore[index]
    assert withdraw_status == 200
    assert after["summary"]["withdrawn"] == 1  # type: ignore[index]
    assert snapshot_status == 201 and snapshot["created"] is True  # type: ignore[index]
    assert changes["tracking"]["total"] == 0  # type: ignore[index]
    assert changes["tracking"]["unlogged"] == 0  # type: ignore[index]


def test_dashboard_syncs_decisions_saved_before_the_log(tmp_path: Path) -> None:
    tracking = tmp_path / "tracking.json"
    tracking.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "profile_id": "profile-default",
                "records": {
                    "job-1": {"job_id": "job-1", "status": "archived"},
                    "job-2": {"job_id": "job-2", "status": "new"},
                },
            }
        ),
        encoding="utf-8",
    )
    server = _server(tmp_path, tracking)
    with _running(server) as base:
        before = _get(f"{base}/api/labels/tracking")
        _, synced = _send(f"{base}/api/labels/tracking", {"action": "sync"})
        after = _get(f"{base}/api/labels/tracking")

    assert [item["job_id"] for item in before["unlogged"]] == ["job-1"]  # type: ignore[union-attr]
    assert synced == {"added": 1}
    assert after["unlogged"] == []
    assert after["rows"][0]["source"] == "sync"  # type: ignore[index]


def test_dashboard_rejects_unknown_tracking_actions(tmp_path: Path) -> None:
    server = _server(tmp_path, tmp_path / "tracking.json")
    with _running(server) as base:
        request = Request(
            f"{base}/api/labels/tracking",
            data=b'{"action": "relabel", "job_id": "job-1"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(Exception, match="400"):
            urlopen(request)  # noqa: S310
        missing = Request(
            f"{base}/api/labels/tracking",
            data=b'{"action": "withdraw", "job_id": "job-9"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(Exception, match="409"):
            urlopen(missing)  # noqa: S310


def test_cli_syncs_summarizes_and_exports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tracking = tmp_path / "tracking.json"
    tracking.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "records": {
                    "job-1": {"job_id": "job-1", "status": "saved"},
                    "job-2": {"job_id": "job-2", "status": "archived"},
                },
            }
        ),
        encoding="utf-8",
    )
    mart = tmp_path / "mart.json"
    mart.write_text(json.dumps(_mart("job-1")), encoding="utf-8")
    common = ["--private-root", str(tmp_path), "--tracking", str(tracking), "--mart", str(mart)]

    assert tracking_cli.main(["sync", *common]) == 0
    assert json.loads(capsys.readouterr().out)["added"] == 2
    assert tracking_cli.main(["summary", *common]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert (summary["positive"], summary["negative"], summary["unlogged"]) == (1, 1, 0)
    assert tracking_cli.main(["snapshot", *common]) == 0
    snapshot_id = json.loads(capsys.readouterr().out)["snapshot"]["snapshot_id"]
    assert tracking_cli.main(["export", "--snapshot", snapshot_id, *common]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert {row["job_id"]: row["label"] for row in exported["rows"]} == {"job-1": 1, "job-2": 0}
    assert tracking_cli.main(["export", "--snapshot", "nope", *common]) == 1
