"""Tests for the Labels tab read model, its orphan alert, and the /api/labels endpoint."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

import pytest
from pydantic import ValidationError

from comparison.label_review import LabelState, orphan_alert, review_labels, summarize_labels
from comparison.labels import RequirementLabel, RequirementLabelStore, labels_path
from comparison.runner import results_path
from dashboard.app import DashboardHTTPServer, create_server
from utils.config import Config, LabelsConfig

SQL_LINE = "3+ years of experience with SQL"
DEGREE_LINE = "Bachelor's degree in Statistics or a related field"
PYTHON_LINE = "Experience with Python"
CLOSED_LINE = "Experience with Tableau"
START = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _item(text: str, status: str, kind: str = "skill") -> dict[str, object]:
    return {"text": text, "required": True, "kind": kind, "status": status}


def _comparisons() -> dict[str, object]:
    return {
        "generated_at": "2026-10-27T10:30:00+00:00",
        "model": {"name": "application_comparison", "version": "1.4.0", "settings_hash": "def"},
        "resume": {"resume_version_id": "resume-v1"},
        "jobs": {
            "job-1": {
                "dimensions": {
                    "requirements": {
                        "items": [
                            _item(SQL_LINE, "met"),
                            _item(DEGREE_LINE, "met", kind="degree"),
                        ]
                    }
                }
            },
            # job-2 is still compared, but its Python line was reworded by a parser change.
            "job-2": {
                "dimensions": {"requirements": {"items": [_item("Python experience", "met")]}}
            },
        },
    }


def _label(
    job_id: str,
    requirement: str,
    predicted: str,
    corrected: str,
    minutes: int,
    *,
    kind: str = "skill",
    version: str = "1.3.0",
) -> RequirementLabel:
    return RequirementLabel(
        job_id=job_id,
        requirement=requirement,
        predicted_status=predicted,
        corrected_status=corrected,
        agrees=predicted == corrected,
        kind=kind,
        required=True,
        model_version=version,
        labeled_at=START + timedelta(minutes=minutes),
    )


def _labels() -> list[RequirementLabel]:
    return [
        # Agreed and the model still says "met": current.
        _label("job-1", SQL_LINE, "met", "met", 1),
        # Labeled "missing" when the model said partial, later corrected to "met"; the
        # model now says met (a vocabulary fix), so the newest label is stale.
        _label("job-1", DEGREE_LINE, "partial", "missing", 2, kind="degree"),
        _label("job-1", DEGREE_LINE, "partial", "met", 5, kind="degree"),
        _label("job-2", PYTHON_LINE, "missing", "met", 3),
        _label("job-3", CLOSED_LINE, "met", "met", 4),
    ]


def _by_requirement(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(row["requirement"]): row for row in rows}


def test_each_label_is_checked_against_the_latest_comparison() -> None:
    postings = {"job-1": {"title": "Data Analyst", "employer": "Example Health"}}

    rows = review_labels(_labels(), _comparisons(), postings)
    by_text = _by_requirement(rows)

    assert len(rows) == 4
    assert [row["requirement"] for row in rows] == [DEGREE_LINE, CLOSED_LINE, PYTHON_LINE, SQL_LINE]
    assert by_text[SQL_LINE]["state"] == LabelState.CURRENT
    assert by_text[SQL_LINE]["title"] == "Data Analyst"
    assert by_text[SQL_LINE]["employer"] == "Example Health"
    assert by_text[DEGREE_LINE]["state"] == LabelState.STALE
    assert by_text[PYTHON_LINE]["state"] == LabelState.ORPHANED
    assert by_text[CLOSED_LINE]["state"] == LabelState.CLOSED
    assert by_text[CLOSED_LINE]["title"] is None


def test_stale_labels_show_then_now_and_keep_their_history() -> None:
    degree = _by_requirement(review_labels(_labels(), _comparisons()))[DEGREE_LINE]

    assert degree["predicted_status"] == "partial"
    assert degree["current_status"] == "met"
    assert degree["corrected_status"] == "met"
    assert degree["agrees"] is False
    assert degree["agrees_now"] is True
    assert degree["source"] == "drawer"
    assert degree["history"] == [
        {
            "action": "label",
            "predicted_status": "partial",
            "corrected_status": "missing",
            "source": "drawer",
            "note": None,
            "model_version": "1.3.0",
            "labeled_at": "2026-10-20T12:02:00+00:00",
        }
    ]


def test_orphaned_and_closed_labels_have_no_current_verdict() -> None:
    by_text = _by_requirement(review_labels(_labels(), _comparisons()))

    for text in (PYTHON_LINE, CLOSED_LINE):
        assert by_text[text]["current_status"] is None
        assert by_text[text]["agrees_now"] is None


def test_without_comparison_results_every_label_counts_as_closed() -> None:
    rows = review_labels(_labels(), None)

    assert {row["state"] for row in rows} == {LabelState.CLOSED}


def test_summary_counts_states_sources_and_kinds() -> None:
    rows = review_labels(_labels(), _comparisons())

    summary = summarize_labels(rows, LabelsConfig())

    assert summary["total"] == 4
    assert summary["agree"] == 2
    assert summary["disagree"] == 2
    assert summary["not_requirement"] == 0
    assert summary["states"] == {
        "current": 1,
        "stale": 1,
        "orphaned": 1,
        "removed": 0,
        "closed": 1,
    }
    assert summary["by_source"] == {"drawer": 4}
    assert summary["by_kind"] == {"skill": 3, "degree": 1}
    assert summary["queue_target_per_group"] == 10


def test_withdrawn_lines_are_listed_but_left_out_of_every_count() -> None:
    labels = [
        *_labels(),
        RequirementLabel(
            action="withdraw",
            source="labels_tab",
            note="duplicate of another line",
            job_id="job-2",
            requirement=PYTHON_LINE,
            predicted_status="missing",
            kind="skill",
            labeled_at=START + timedelta(minutes=9),
        ),
    ]

    rows = review_labels(labels, _comparisons())
    python = _by_requirement(rows)[PYTHON_LINE]
    summary = summarize_labels(rows, LabelsConfig())

    assert rows[0]["requirement"] == PYTHON_LINE
    assert python["withdrawn"] is True
    assert python["withdrawal_note"] == "duplicate of another line"
    assert python["corrected_status"] == "met"
    assert python["history"] == []
    assert summary["total"] == 3
    assert summary["withdrawn"] == 1
    assert summary["states"]["orphaned"] == 0
    assert summary["by_kind"] == {"skill": 2, "degree": 1}


def test_not_a_requirement_is_counted_apart_from_agree_and_disagree() -> None:
    labels = [*_labels(), _label("job-1", SQL_LINE, "met", "not_requirement", 8)]

    rows = review_labels(labels, _comparisons())
    sql = _by_requirement(rows)[SQL_LINE]
    summary = summarize_labels(rows, LabelsConfig())

    assert sql["not_requirement"] is True
    assert sql["corrected_status"] == "not_requirement"
    assert sql["history"][0]["corrected_status"] == "met"
    assert _by_requirement(rows)[DEGREE_LINE]["not_requirement"] is False
    assert summary["total"] == 4
    assert summary["not_requirement"] == 1
    # Before the mark: 2 agreed (SQL, Tableau) and 2 disagreed. SQL leaves agree only.
    assert summary["agree"] == 1
    assert summary["disagree"] == 2


def test_a_filtered_not_a_requirement_line_is_removed_not_orphaned() -> None:
    labels = [
        *_labels(),
        _label("job-2", "as far in advance as possible.", "partial", "not_requirement", 8),
    ]

    rows = review_labels(labels, _comparisons())
    summary = summarize_labels(rows, LabelsConfig())

    assert _by_requirement(rows)["as far in advance as possible."]["state"] == LabelState.REMOVED
    assert summary["states"]["removed"] == 1
    assert summary["states"]["orphaned"] == 1
    assert summary["orphan_alert"]["orphaned"] == 1


def test_relabel_after_withdrawal_keeps_the_withdrawal_in_history() -> None:
    labels = [
        _label("job-1", SQL_LINE, "met", "missing", 1),
        RequirementLabel(
            action="withdraw",
            job_id="job-1",
            requirement=SQL_LINE,
            predicted_status="met",
            labeled_at=START + timedelta(minutes=2),
        ),
        _label("job-1", SQL_LINE, "met", "met", 3),
    ]

    [row] = review_labels(labels, _comparisons())

    assert row["withdrawn"] is False
    assert row["agrees"] is True
    assert [entry["action"] for entry in row["history"]] == ["withdraw", "label"]


def test_sources_are_counted_separately() -> None:
    labels = [
        _label("job-1", SQL_LINE, "met", "met", 1),
        RequirementLabel(
            source="queue",
            sampled=True,
            job_id="job-1",
            requirement=DEGREE_LINE,
            predicted_status="met",
            corrected_status="met",
            agrees=True,
            labeled_at=START,
        ),
    ]

    summary = summarize_labels(review_labels(labels, _comparisons()), LabelsConfig())

    assert summary["by_source"] == {"drawer": 1, "queue": 1}
    assert summary["sampled"] == 1


def test_orphan_alert_fires_on_count() -> None:
    settings = LabelsConfig.model_validate({"orphans": {"alert_count": 3, "alert_share": None}})

    below = orphan_alert(2, 100, settings)
    at = orphan_alert(3, 100, settings)

    assert below["active"] is False
    assert at["active"] is True
    assert at["reasons"] == ["3 orphaned labels (alert at 3)"]


def test_orphan_alert_share_waits_for_enough_labels() -> None:
    settings = LabelsConfig.model_validate(
        {"orphans": {"alert_count": 50, "alert_share": 0.1, "share_minimum_labels": 20}}
    )

    assert orphan_alert(1, 2, settings)["active"] is False
    assert orphan_alert(1, 19, settings)["active"] is False
    alert = orphan_alert(2, 20, settings)
    assert alert["active"] is True
    assert alert["share"] == 0.1
    assert alert["reasons"] == ["10% of labels orphaned (alert at 10%)"]


def test_closed_postings_never_raise_the_orphan_alert() -> None:
    labels = [_label(f"job-closed-{index}", SQL_LINE, "met", "met", index) for index in range(30)]
    settings = LabelsConfig.model_validate({"orphans": {"alert_count": 1}})

    summary = summarize_labels(review_labels(labels, _comparisons()), settings)

    assert summary["states"]["closed"] == 30
    assert summary["orphan_alert"]["active"] is False


def test_labels_config_ships_with_the_project_and_rejects_unknown_keys() -> None:
    settings = Config().labels

    assert settings.orphans.alert_count == 10
    assert settings.orphans.alert_share == 0.1
    assert settings.queue.target_per_group == 10
    assert settings.queue.exclude_kinds == ["trait"]
    with pytest.raises(ValidationError):
        LabelsConfig.model_validate({"queue": {"target_per_group": 0}})
    with pytest.raises(ValidationError):
        LabelsConfig.model_validate({"orphans": {"alert_after": 5}})


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


def test_dashboard_lists_labels_with_states_and_posting_titles(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    results = results_path(private_root)
    results.parent.mkdir(parents=True)
    results.write_text(json.dumps(_comparisons()), encoding="utf-8")
    store = RequirementLabelStore(labels_path(private_root))
    for label in _labels():
        store.append(label)
    mart_path = tmp_path / "mart.json"
    mart_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-10-27T10:00:00+00:00",
                "row_count": 1,
                "opportunities": [
                    {"job_id": "job-1", "title": "Data Analyst", "employer": "Example Health"}
                ],
            }
        ),
        encoding="utf-8",
    )
    server = create_server(
        port=0,
        private_root=private_root,
        tracking_path=tmp_path / "tracking.json",
        mart_path=mart_path,
    )
    with _running(server) as base_url, urlopen(f"{base_url}/api/labels") as response:  # noqa: S310
        payload = json.loads(response.read())

    rows = _by_requirement(payload["labels"])
    assert payload["summary"]["total"] == 4
    assert payload["summary"]["states"]["orphaned"] == 1
    assert payload["comparison_model"]["version"] == "1.4.0"
    assert rows[SQL_LINE]["title"] == "Data Analyst"
    assert rows[DEGREE_LINE]["state"] == "stale"


def test_dashboard_labels_endpoint_works_before_any_label(tmp_path: Path) -> None:
    server = create_server(
        port=0,
        private_root=tmp_path / "private",
        tracking_path=tmp_path / "tracking.json",
        mart_path=tmp_path / "missing-mart.json",
    )
    with _running(server) as base_url, urlopen(f"{base_url}/api/labels") as response:  # noqa: S310
        payload = json.loads(response.read())

    assert payload["labels"] == []
    assert payload["summary"]["total"] == 0
    assert payload["summary"]["orphan_alert"]["active"] is False
    assert payload["comparison_generated_at"] is None


def test_review_does_not_mutate_the_comparison() -> None:
    comparisons = _comparisons()
    original = deepcopy(comparisons)

    review_labels(_labels(), comparisons)

    assert comparisons == original
