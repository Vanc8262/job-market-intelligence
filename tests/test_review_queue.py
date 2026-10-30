"""Tests for the Labels review queue: seeded stratified sampling and server-checked labels."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from comparison.labels import LabelSource, RequirementLabelStore, labels_path
from comparison.review_queue import (
    build_queue,
    is_queue_head,
    public_queue,
    queue_candidates,
    sample_rank,
)
from comparison.runner import results_path
from dashboard.app import DashboardHTTPServer, create_server
from utils.config import LabelsConfig


def _item(text: str, status: str, kind: str = "skill") -> dict[str, object]:
    return {"text": text, "required": True, "kind": kind, "status": status}


def _comparisons(jobs: int = 4) -> dict[str, object]:
    """Each job has skill lines (met and missing), a degree line, and a trait line."""
    return {
        "generated_at": "2026-10-28T10:00:00+00:00",
        "model": {"version": "1.4.0"},
        "jobs": {
            f"job-{index}": {
                "dimensions": {
                    "requirements": {
                        "items": [
                            _item(f"Experience with SQL ({index})", "met"),
                            _item(f"Experience with Spark ({index})", "missing"),
                            _item(f"Bachelor's degree ({index})", "partial", kind="degree"),
                            _item(f"Curious ({index})", "self_assess", kind="trait"),
                        ]
                    }
                }
            }
            for index in range(jobs)
        },
    }


def _settings(target: int = 2, seed: int = 7) -> LabelsConfig:
    return LabelsConfig.model_validate({"queue": {"target_per_group": target, "seed": seed}})


def test_candidates_are_grouped_by_kind_and_verdict_without_traits() -> None:
    groups = queue_candidates(_comparisons(), _settings())

    assert set(groups) == {("skill", "met"), ("skill", "missing"), ("degree", "partial")}
    assert all(len(pool) == 4 for pool in groups.values())


def test_order_is_seeded_reproducible_and_independent_of_other_postings() -> None:
    first = [c.requirement for c in queue_candidates(_comparisons(), _settings())[("skill", "met")]]
    again = [c.requirement for c in queue_candidates(_comparisons(), _settings())[("skill", "met")]]
    other_seed = [
        c.requirement for c in queue_candidates(_comparisons(), _settings(seed=8))[("skill", "met")]
    ]
    # Adding postings only inserts lines; existing lines keep their relative order.
    more = [
        c.requirement
        for c in queue_candidates(_comparisons(jobs=8), _settings())[("skill", "met")]
        if c.requirement in first
    ]

    assert first == again == more
    assert sorted(first) == sorted(other_seed)
    assert first != other_seed
    assert sample_rank(7, "job-1", "x") == sample_rank(7, "job-1", "x")


def test_queue_serves_the_least_filled_group_and_stops_at_target(tmp_path: Path) -> None:
    store = RequirementLabelStore(labels_path(tmp_path))
    comparisons = _comparisons()
    settings = _settings(target=2)

    queue = build_queue(comparisons, store.all(), settings)
    assert queue["labeled"] == 0
    assert queue["target"] == 6
    served = []
    while not queue["complete"]:
        nxt = queue["next"]
        assert isinstance(nxt, dict)
        served.append((nxt["kind"], nxt["status"]))
        store.record(
            comparisons,
            str(nxt["job_id"]),
            str(nxt["requirement"]),
            str(nxt["status"]),
            source=LabelSource.QUEUE,
            sample_seed=settings.queue.seed,
        )
        queue = build_queue(comparisons, store.all(), settings)

    # Groups fill evenly: every group gets its first label before any gets a second.
    assert len(set(served[:3])) == 3
    assert queue["labeled"] == 6
    assert {(g["kind"], g["status"], g["labeled"], g["target"]) for g in queue["groups"]} == {
        ("degree", "partial", 2, 2),
        ("skill", "met", 2, 2),
        ("skill", "missing", 2, 2),
    }


def test_sampled_not_a_requirement_labels_count_toward_their_group(tmp_path: Path) -> None:
    store = RequirementLabelStore(labels_path(tmp_path))
    comparisons = _comparisons()
    settings = _settings(target=2)
    nxt = build_queue(comparisons, store.all(), settings)["next"]
    assert isinstance(nxt, dict)

    store.record(
        comparisons,
        str(nxt["job_id"]),
        str(nxt["requirement"]),
        "not_requirement",
        source=LabelSource.QUEUE,
        sample_seed=settings.queue.seed,
    )
    queue = build_queue(comparisons, store.all(), settings)

    group = next(
        g for g in queue["groups"] if (g["kind"], g["status"]) == (nxt["kind"], nxt["status"])
    )
    assert group["labeled"] == 1
    assert queue["next"] != nxt


def test_drawer_labels_do_not_fill_the_queue(tmp_path: Path) -> None:
    store = RequirementLabelStore(labels_path(tmp_path))
    comparisons = _comparisons()
    settings = _settings(target=1)
    head = build_queue(comparisons, [], settings)["next"]
    assert isinstance(head, dict)

    store.record(comparisons, str(head["job_id"]), str(head["requirement"]), "met")
    queue = build_queue(comparisons, store.all(), settings)

    assert queue["labeled"] == 0
    # The same randomly drawn line is still served: the drawer label was not sampled.
    assert is_queue_head(queue, str(head["job_id"]), str(head["requirement"]))


def test_withdrawn_queue_labels_reopen_their_line(tmp_path: Path) -> None:
    store = RequirementLabelStore(labels_path(tmp_path))
    comparisons = _comparisons()
    settings = _settings(target=1)
    head = build_queue(comparisons, [], settings)["next"]
    assert isinstance(head, dict)
    job_id, text = str(head["job_id"]), str(head["requirement"])
    store.record(comparisons, job_id, text, "met", source=LabelSource.QUEUE, sample_seed=7)
    assert not is_queue_head(build_queue(comparisons, store.all(), settings), job_id, text)

    store.withdraw(job_id, text)

    assert is_queue_head(build_queue(comparisons, store.all(), settings), job_id, text)


def test_small_groups_cap_their_target_and_closed_labels_still_count(tmp_path: Path) -> None:
    store = RequirementLabelStore(labels_path(tmp_path))
    settings = _settings(target=10)
    comparisons = _comparisons(jobs=2)
    queue = build_queue(comparisons, [], settings)
    assert {g["target"] for g in queue["groups"]} == {2}

    head = queue["next"]
    assert isinstance(head, dict)
    store.record(
        comparisons,
        str(head["job_id"]),
        str(head["requirement"]),
        "met",
        source=LabelSource.QUEUE,
        sample_seed=7,
    )
    # The posting closes: its sampled label still counts toward the group.
    later = build_queue(_comparisons(jobs=0), store.all(), settings)

    assert later["labeled"] == 1
    assert later["complete"] is True
    assert "heads" not in public_queue(later)


def test_empty_comparison_gives_an_empty_complete_queue() -> None:
    queue = build_queue(None, [], _settings())

    assert queue["groups"] == []
    assert queue["next"] is None
    assert queue["complete"] is True


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


def _get(url: str) -> dict[str, object]:
    with urlopen(url) as response:  # noqa: S310
        payload = json.loads(response.read())
    assert isinstance(payload, dict)
    return payload


def test_dashboard_queue_marks_only_the_served_line_as_sampled(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    results = results_path(private_root)
    results.parent.mkdir(parents=True)
    results.write_text(json.dumps(_comparisons()), encoding="utf-8")
    server = create_server(
        port=0, private_root=private_root, tracking_path=tmp_path / "tracking.json"
    )
    with _running(server) as base_url:
        endpoint = f"{base_url}/api/labels/queue"
        queue = _get(endpoint)
        head = queue["next"]
        assert isinstance(head, dict)
        line = {"job_id": head["job_id"], "requirement": head["requirement"]}
        created, label = _post(endpoint, {**line, "corrected_status": "met", "note": "clear"})
        repeat, _ = _post(endpoint, {**line, "corrected_status": "met"})
        # A line the sampler is not serving cannot be saved as sampled.
        unserved, _ = _post(
            endpoint,
            {"job_id": "job-0", "requirement": "Curious (0)", "corrected_status": "met"},
        )
        after = _get(endpoint)
        fresh = after["next"]
        assert isinstance(fresh, dict)
        bad_status, _ = _post(
            endpoint,
            {
                "job_id": fresh["job_id"],
                "requirement": fresh["requirement"],
                "corrected_status": "great",
            },
        )
        labels = _get(f"{base_url}/api/labels")

    assert "heads" not in queue
    assert queue["target"] == 12
    assert created == 201
    assert (label["source"], label["sampled"], label["sample_seed"]) == ("queue", True, 20261028)
    assert label["note"] == "clear"
    assert (repeat, unserved, bad_status) == (409, 409, 400)
    assert after["labeled"] == 1
    summary = labels["summary"]
    assert isinstance(summary, dict)
    assert summary["by_source"] == {"queue": 1}
    assert summary["sampled"] == 1
