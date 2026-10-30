"""Tests for owner corrections of Phase 6.3 requirement verdicts (private labels)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import ValidationError

from comparison.labels import (
    LabelSource,
    RequirementLabel,
    RequirementLabelStore,
    find_requirement,
    labels_path,
)
from comparison.runner import results_path
from dashboard.app import DashboardHTTPServer, create_server

SQL_LINE = "3+ years of experience with SQL"
DEGREE_LINE = "Bachelor's degree in Statistics or a related field"
TRAIT_LINE = "Curious and comfortable with ambiguity"


def _comparisons() -> dict[str, object]:
    return {
        "generated_at": "2026-10-20T10:30:00+00:00",
        "model": {"name": "application_comparison", "version": "1.3.0", "settings_hash": "abc123"},
        "resume": {"resume_version_id": "resume-v1", "extraction_id": "ext-1", "years": 4},
        "jobs": {
            "job-1": {
                "fit_score": 72.0,
                "band": "moderate",
                "dimensions": {
                    "requirements": {
                        "score": 0.6,
                        "items": [
                            {"text": SQL_LINE, "required": True, "kind": "skill", "status": "met"},
                            {
                                "text": DEGREE_LINE,
                                "required": True,
                                "kind": "degree",
                                "status": "partial",
                            },
                            {
                                "text": TRAIT_LINE,
                                "required": False,
                                "kind": "trait",
                                "status": "self_assess",
                            },
                        ],
                    }
                },
            }
        },
    }


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_record_takes_the_prediction_from_the_stored_comparison(tmp_path: Path) -> None:
    store = RequirementLabelStore(labels_path(tmp_path))
    labeled_at = datetime(2026, 10, 23, 17, 0, tzinfo=UTC)

    label = store.record(_comparisons(), "job-1", DEGREE_LINE, "missing", now=labeled_at)

    assert store.path == tmp_path / "labels" / "requirement_labels.jsonl"
    assert label.predicted_status == "partial"
    assert label.corrected_status == "missing"
    assert label.agrees is False
    [line] = _lines(store.path)
    assert line == {
        "schema_version": 2,
        "action": "label",
        "source": "drawer",
        "sampled": False,
        "sample_seed": None,
        "note": None,
        "job_id": "job-1",
        "requirement": DEGREE_LINE,
        "predicted_status": "partial",
        "corrected_status": "missing",
        "agrees": False,
        "kind": "degree",
        "required": True,
        "model_version": "1.3.0",
        "settings_hash": "abc123",
        "resume_version_id": "resume-v1",
        "labeled_at": "2026-10-23T17:00:00Z",
    }


def test_agreeing_records_the_predicted_status(tmp_path: Path) -> None:
    store = RequirementLabelStore(labels_path(tmp_path))

    label = store.record(_comparisons(), "job-1", SQL_LINE, "met")

    assert label.agrees is True
    assert label.corrected_status == label.predicted_status == "met"


def test_self_assess_lines_can_be_labeled(tmp_path: Path) -> None:
    store = RequirementLabelStore(labels_path(tmp_path))

    label = store.record(_comparisons(), "job-1", TRAIT_LINE, "met")

    assert label.predicted_status == "self_assess"
    assert label.kind == "trait"
    assert label.agrees is False


def test_log_is_append_only_and_the_newest_label_wins(tmp_path: Path) -> None:
    store = RequirementLabelStore(labels_path(tmp_path))
    store.record(_comparisons(), "job-1", DEGREE_LINE, "missing")
    store.record(_comparisons(), "job-1", SQL_LINE, "met")
    store.record(_comparisons(), "job-1", DEGREE_LINE, "partial")

    history = [line["corrected_status"] for line in _lines(store.path)]
    latest = store.latest_for("job-1")

    assert history == ["missing", "met", "partial"]
    assert latest[DEGREE_LINE].corrected_status == "partial"
    assert latest[DEGREE_LINE].agrees is True
    assert store.latest_for("job-2") == {}


def test_unknown_requirements_and_statuses_are_rejected(tmp_path: Path) -> None:
    store = RequirementLabelStore(labels_path(tmp_path))

    with pytest.raises(LookupError):
        store.record(_comparisons(), "job-1", "A line the model never saw", "met")
    with pytest.raises(LookupError):
        store.record(_comparisons(), "job-404", SQL_LINE, "met")
    with pytest.raises(LookupError):
        store.record(None, "job-1", SQL_LINE, "met")
    with pytest.raises(ValueError, match="met, partial, missing, or not_requirement"):
        store.record(_comparisons(), "job-1", SQL_LINE, "self_assess")
    assert not store.path.exists()


def test_lines_the_parser_should_not_have_picked_up_can_be_marked(tmp_path: Path) -> None:
    store = RequirementLabelStore(labels_path(tmp_path))

    label = store.record(_comparisons(), "job-1", SQL_LINE, "not_requirement", note="perks")

    assert label.corrected_status == "not_requirement"
    assert label.not_requirement is True
    assert label.agrees is False
    assert store.latest_for("job-1")[SQL_LINE].not_requirement is True
    # Withdrawing the mark puts the line back to unlabeled, like any other label.
    store.withdraw("job-1", SQL_LINE)
    assert store.latest_for("job-1") == {}


def test_a_torn_final_line_is_skipped_but_other_damage_is_reported(tmp_path: Path) -> None:
    store = RequirementLabelStore(labels_path(tmp_path))
    store.record(_comparisons(), "job-1", SQL_LINE, "met")
    with store.path.open("a", encoding="utf-8") as stream:
        stream.write('{"schema_version": 1, "job_id": "job-1", "requ')

    assert [label.requirement for label in store.all()] == [SQL_LINE]

    store.path.write_text('{"broken": true}\n' + store.path.read_text(), encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        store.all()


def test_find_requirement_matches_exact_text() -> None:
    assert find_requirement(_comparisons(), "job-1", SQL_LINE) == {
        "text": SQL_LINE,
        "required": True,
        "kind": "skill",
        "status": "met",
    }
    assert find_requirement(_comparisons(), "job-1", SQL_LINE.upper()) is None


V1_LINE = {
    "schema_version": 1,
    "job_id": "job-1",
    "requirement": SQL_LINE,
    "predicted_status": "met",
    "corrected_status": "met",
    "agrees": True,
    "kind": "skill",
    "required": True,
    "model_version": "1.3.0",
    "settings_hash": "abc123",
    "resume_version_id": "resume-v1",
    "labeled_at": "2026-10-23T17:00:00Z",
}


def test_schema_v1_lines_still_load_as_drawer_labels(tmp_path: Path) -> None:
    store = RequirementLabelStore(labels_path(tmp_path))
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps(V1_LINE) + "\n", encoding="utf-8")
    store.record(_comparisons(), "job-1", DEGREE_LINE, "missing", source=LabelSource.LABELS_TAB)

    old, new = store.all()

    assert (old.schema_version, old.action, old.source, old.sampled) == (
        1,
        "label",
        "drawer",
        False,
    )
    assert (new.schema_version, new.source) == (2, "labels_tab")
    assert set(store.latest_for("job-1")) == {SQL_LINE, DEGREE_LINE}


def test_notes_are_trimmed_and_capped(tmp_path: Path) -> None:
    store = RequirementLabelStore(labels_path(tmp_path))

    label = store.record(
        _comparisons(), "job-1", DEGREE_LINE, "missing", note="  degree in\nEconomics  "
    )
    blank = store.record(_comparisons(), "job-1", SQL_LINE, "met", note="   ")

    assert label.note == "degree in Economics"
    assert blank.note is None
    with pytest.raises(ValidationError):
        store.record(_comparisons(), "job-1", SQL_LINE, "met", note="x" * 501)


def test_withdraw_hides_the_line_and_relabeling_restores_it(tmp_path: Path) -> None:
    store = RequirementLabelStore(labels_path(tmp_path))
    store.record(_comparisons(), "job-1", DEGREE_LINE, "missing")

    withdrawal = store.withdraw("job-1", DEGREE_LINE, note="misread the line")

    assert withdrawal.action == "withdraw"
    assert withdrawal.corrected_status is None and withdrawal.agrees is None
    assert withdrawal.predicted_status == "partial"
    assert withdrawal.kind == "degree"
    assert withdrawal.note == "misread the line"
    assert store.latest_for("job-1") == {}
    with pytest.raises(LookupError):
        store.withdraw("job-1", DEGREE_LINE)

    store.record(_comparisons(), "job-1", DEGREE_LINE, "partial")

    assert store.latest_for("job-1")[DEGREE_LINE].corrected_status == "partial"
    assert [line["action"] for line in _lines(store.path)] == ["label", "withdraw", "label"]


def test_withdraw_works_for_lines_missing_from_the_comparison(tmp_path: Path) -> None:
    store = RequirementLabelStore(labels_path(tmp_path))
    store.record(_comparisons(), "job-1", SQL_LINE, "met")

    # The posting closed: the label can still be withdrawn from the log.
    store.withdraw("job-1", SQL_LINE)

    assert store.latest_for("job-1") == {}
    with pytest.raises(LookupError):
        store.withdraw("job-404", SQL_LINE)


def test_label_entries_must_be_consistent() -> None:
    base = {**V1_LINE, "schema_version": 2}
    with pytest.raises(ValidationError, match="corrected_status"):
        RequirementLabel.model_validate({**base, "corrected_status": None})
    with pytest.raises(ValidationError, match="no verdict"):
        RequirementLabel.model_validate({**base, "action": "withdraw"})
    with pytest.raises(ValidationError, match="sampled"):
        RequirementLabel.model_validate({**base, "sampled": True})
    with pytest.raises(ValidationError, match="v1"):
        RequirementLabel.model_validate({**V1_LINE, "source": "queue"})
    assert RequirementLabel.model_validate({**base, "source": "queue", "sampled": True}).sampled


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


def _post(url: str, payload: object) -> tuple[int, dict[str, object]]:
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request) as response:  # noqa: S310
            return int(response.status), json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_dashboard_saves_labels_and_returns_them_with_the_comparison(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    results = results_path(private_root)
    results.parent.mkdir(parents=True)
    results.write_text(json.dumps(_comparisons()), encoding="utf-8")
    server = create_server(
        port=0, private_root=private_root, tracking_path=tmp_path / "tracking.json"
    )
    with _running(server) as base_url:
        endpoint = f"{base_url}/api/requirement-labels"
        created, saved = _post(
            endpoint, {"job_id": "job-1", "requirement": DEGREE_LINE, "corrected_status": "met"}
        )
        unknown, _ = _post(
            endpoint, {"job_id": "job-1", "requirement": "Not a line", "corrected_status": "met"}
        )
        invalid, _ = _post(
            endpoint, {"job_id": "job-1", "requirement": SQL_LINE, "corrected_status": "great"}
        )
        missing_field, _ = _post(endpoint, {"job_id": "job-1", "corrected_status": "met"})
        spoofed, _ = _post(
            endpoint,
            {
                "job_id": "job-1",
                "requirement": SQL_LINE,
                "corrected_status": "met",
                "predicted_status": "missing",
            },
        )
        with urlopen(f"{base_url}/api/comparisons/job-1") as response:  # noqa: S310
            detail = json.loads(response.read())

    assert created == 201
    assert saved["predicted_status"] == "partial"
    assert saved["corrected_status"] == "met"
    assert (unknown, invalid, missing_field) == (404, 400, 400)
    # The client cannot override the prediction; it always comes from the stored result.
    assert spoofed == 201
    assert detail["labels"][DEGREE_LINE]["corrected_status"] == "met"
    assert detail["labels"][DEGREE_LINE]["agrees"] is False
    assert detail["labels"][SQL_LINE]["predicted_status"] == "met"
    assert detail["labels"][SQL_LINE]["agrees"] is True
    assert [line["requirement"] for line in _lines(labels_path(private_root))] == [
        DEGREE_LINE,
        SQL_LINE,
    ]


def test_dashboard_withdraws_labels_and_never_trusts_source_claims(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    results = results_path(private_root)
    results.parent.mkdir(parents=True)
    results.write_text(json.dumps(_comparisons()), encoding="utf-8")
    server = create_server(
        port=0, private_root=private_root, tracking_path=tmp_path / "tracking.json"
    )
    with _running(server) as base_url:
        endpoint = f"{base_url}/api/requirement-labels"
        line = {"job_id": "job-1", "requirement": DEGREE_LINE}
        created, saved = _post(
            endpoint,
            {**line, "corrected_status": "missing", "source": "labels_tab", "note": "Econ"},
        )
        queue_claim, _ = _post(endpoint, {**line, "corrected_status": "met", "source": "queue"})
        sampled_claim, _ = _post(endpoint, {**line, "corrected_status": "met", "sampled": True})
        bad_action, _ = _post(endpoint, {**line, "action": "delete"})
        bad_note, _ = _post(endpoint, {**line, "corrected_status": "met", "note": 5})
        withdrawn, withdrawal = _post(endpoint, {**line, "action": "withdraw"})
        again, _ = _post(endpoint, {**line, "action": "withdraw"})
        with urlopen(f"{base_url}/api/comparisons/job-1") as response:  # noqa: S310
            detail = json.loads(response.read())

    assert created == 201
    assert (saved["source"], saved["note"], saved["sampled"]) == ("labels_tab", "Econ", False)
    assert (queue_claim, sampled_claim, bad_action, bad_note) == (400, 400, 400, 400)
    assert withdrawn == 201
    assert withdrawal["action"] == "withdraw"
    assert withdrawal["source"] == "drawer"
    assert again == 404
    assert DEGREE_LINE not in detail["labels"]
    assert [entry["action"] for entry in _lines(labels_path(private_root))] == [
        "label",
        "withdraw",
    ]


def test_dashboard_accepts_not_a_requirement_from_the_drawer(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    results = results_path(private_root)
    results.parent.mkdir(parents=True)
    results.write_text(json.dumps(_comparisons()), encoding="utf-8")
    server = create_server(
        port=0, private_root=private_root, tracking_path=tmp_path / "tracking.json"
    )
    with _running(server) as base_url:
        created, saved = _post(
            f"{base_url}/api/requirement-labels",
            {"job_id": "job-1", "requirement": SQL_LINE, "corrected_status": "not_requirement"},
        )
        with urlopen(f"{base_url}/api/labels") as response:  # noqa: S310
            labels = json.loads(response.read())

    assert created == 201
    assert saved["corrected_status"] == "not_requirement"
    assert labels["summary"]["not_requirement"] == 1
    assert labels["summary"]["disagree"] == 0
    assert labels["labels"][0]["not_requirement"] is True
