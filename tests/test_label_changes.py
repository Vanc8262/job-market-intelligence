"""Tests for the Labels Changes view: change log, snapshots, and tracking-label progress."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen

import pytest

from comparison.label_changes import (
    create_snapshot,
    daily_counts,
    label_events,
    list_snapshots,
    snapshot_labels,
    tracking_progress,
)
from comparison.labels import RequirementLabel, RequirementLabelStore, labels_path
from dashboard.app import DashboardHTTPServer, create_server
from scripts import label_snapshots
from utils.config import LabelsConfig

SQL_LINE = "3+ years of experience with SQL"
DEGREE_LINE = "Bachelor's degree in Statistics or a related field"
START = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _label(
    requirement: str,
    corrected: str | None,
    hours: float,
    *,
    action: str = "label",
    version: str = "1.3.0",
    source: str = "drawer",
) -> RequirementLabel:
    return RequirementLabel(
        action=action,
        source=source,
        sampled=source == "queue",
        sample_seed=7 if source == "queue" else None,
        job_id="job-1",
        requirement=requirement,
        predicted_status="partial",
        corrected_status=corrected,
        agrees=None if corrected is None else corrected == "partial",
        kind="degree" if requirement == DEGREE_LINE else "skill",
        model_version=version,
        labeled_at=START + timedelta(hours=hours),
    )


def _history() -> list[RequirementLabel]:
    return [
        _label(DEGREE_LINE, "missing", 0),
        _label(SQL_LINE, "met", 1),
        _label(DEGREE_LINE, "met", 2),
        _label(DEGREE_LINE, "met", 3),
        _label(DEGREE_LINE, None, 26, action="withdraw", version="1.4.0"),
        _label(DEGREE_LINE, "partial", 27, version="1.4.0", source="queue"),
    ]


def test_every_log_entry_becomes_a_typed_event() -> None:
    events = label_events(_history())

    assert [event["type"] for event in events] == [
        "added",
        "added",
        "changed",
        "confirmed",
        "model_version",
        "withdrawn",
        "restored",
    ]
    changed = events[2]
    assert (changed["from_status"], changed["to_status"]) == ("missing", "met")
    assert events[4]["model_version"] == "1.4.0"
    assert events[5]["from_status"] == "met"
    assert events[6]["sampled"] is True


def test_daily_counts_are_newest_first() -> None:
    days = daily_counts(label_events(_history()))

    assert days == [
        {
            "date": "2026-10-21",
            "added": 0,
            "changed": 0,
            "confirmed": 0,
            "withdrawn": 1,
            "restored": 1,
            "sampled": 1,
        },
        {
            "date": "2026-10-20",
            "added": 2,
            "changed": 1,
            "confirmed": 1,
            "withdrawn": 0,
            "restored": 0,
            "sampled": 0,
        },
    ]


def test_tracking_progress_counts_preferences_but_not_rejections() -> None:
    settings = LabelsConfig()
    statuses = ["saved"] * 6 + ["archived"] * 3 + ["rejected", "new", "applied"]

    progress = tracking_progress(statuses, settings)

    assert (progress["positive"], progress["negative"], progress["total"]) == (7, 3, 10)
    assert progress["simulator"] == {"minimum": 10, "minimum_each": 3, "ready": True}
    assert progress["b3"] == {"minimum": 30, "target": 50, "ready": False}
    assert tracking_progress(["saved"] * 10, settings)["simulator"]["ready"] is False


def _store(tmp_path: Path) -> RequirementLabelStore:
    store = RequirementLabelStore(labels_path(tmp_path))
    for label in _history():
        store.append(label)
    return store


def test_snapshots_are_idempotent_and_reproduce_their_labels(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = LabelsConfig()

    manifest, created = create_snapshot(store, settings, {"version": "1.4.0"}, now=START)
    again, created_again = create_snapshot(store, settings, {"version": "1.4.0"})
    store.append(_label(SQL_LINE, "partial", 30))
    later, created_later = create_snapshot(store, settings, None, now=START + timedelta(days=2))

    assert created is True and created_again is False and created_later is True
    assert again["snapshot_id"] == manifest["snapshot_id"]
    assert manifest["log_lines"] == 6
    assert manifest["active_labels"] == 2
    assert manifest["sampled_labels"] == 1
    assert manifest["by_source"] == {"drawer": 1, "queue": 1}
    assert manifest["comparison_model_version"] == "1.4.0"
    assert later["log_lines"] == 7
    assert [item["snapshot_id"] for item in list_snapshots(store)] == [
        later["snapshot_id"],
        manifest["snapshot_id"],
    ]
    assert all(item["verified"] for item in list_snapshots(store))
    assert len(snapshot_labels(store, manifest)) == 6
    # Manifests carry counts and hashes, never requirement text.
    assert SQL_LINE not in json.dumps(list_snapshots(store))


def test_a_torn_final_line_is_not_snapshotted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.path.open("a", encoding="utf-8") as stream:
        stream.write('{"schema_version": 2, "job_')

    manifest, _ = create_snapshot(store, LabelsConfig())

    assert manifest["log_lines"] == 6
    assert list_snapshots(store)[0]["verified"] is True


def test_editing_the_log_breaks_verification(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest, _ = create_snapshot(store, LabelsConfig())

    store.path.write_text(
        store.path.read_text(encoding="utf-8").replace('"missing"', '"met"', 1), encoding="utf-8"
    )

    assert list_snapshots(store)[0]["verified"] is False
    with pytest.raises(ValueError, match="no longer matches"):
        snapshot_labels(store, manifest)


def test_snapshot_cli_creates_lists_and_verifies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _store(tmp_path)
    root = ["--private-root", str(tmp_path)]

    assert label_snapshots.main(["create", *root]) == 0
    created = json.loads(capsys.readouterr().out)
    assert label_snapshots.main(["verify", *root]) == 0
    assert json.loads(capsys.readouterr().out) == {"snapshots": 1, "unverified": []}

    log = labels_path(tmp_path)
    log.write_text(log.read_text(encoding="utf-8").replace("1.3.0", "9.9.9"), encoding="utf-8")
    assert label_snapshots.main(["verify", *root]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["unverified"] == [created["snapshot"]["snapshot_id"]]


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


def _post(url: str) -> tuple[int, dict[str, object]]:
    request = Request(url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request) as response:  # noqa: S310
        return int(response.status), json.loads(response.read())


def test_dashboard_serves_changes_and_saves_snapshots(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    _store(private_root)
    tracking = tmp_path / "tracking.json"
    server = create_server(port=0, private_root=private_root, tracking_path=tracking)
    with _running(server) as base_url:
        first_status, first = _post(f"{base_url}/api/labels/snapshots")
        second_status, second = _post(f"{base_url}/api/labels/snapshots")
        with urlopen(f"{base_url}/api/labels/changes") as response:  # noqa: S310
            changes = json.loads(response.read())

    assert (first_status, first["created"]) == (201, True)
    assert (second_status, second["created"]) == (200, False)
    assert changes["event_count"] == 7
    assert changes["events"][0]["type"] == "restored"
    assert len(changes["days"]) == 2
    assert [item["verified"] for item in changes["snapshots"]] == [True]
    assert changes["tracking"]["total"] == 0
