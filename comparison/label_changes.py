"""Changes view and snapshots for the label log: what changed, when, and which set was used.

The label log is append-only, so its history is the change log: every entry is an
event (a line labeled for the first time, a verdict changed or confirmed, a withdrawal,
a restore), and the first entry made under a new comparison model version marks that
version's arrival.

A snapshot freezes the labeled set that a Phase 7 number was computed from. Because the
log only grows, a snapshot is simply its first ``log_lines`` complete lines; the manifest
stores their SHA-256 so the exact set can be re-read and checked later, and a snapshot
whose prefix no longer matches (the log was edited by hand) is reported as unverified.
Manifests hold counts and hashes only, never requirement text or resume text.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from comparison.labels import LabelAction, RequirementLabel, RequirementLabelStore
from utils.config import LabelsConfig

SNAPSHOT_DIR = "snapshots"


def label_events(labels: Sequence[RequirementLabel]) -> list[dict[str, object]]:
    """Return one event per log entry, oldest first, plus model-version arrivals."""
    events: list[dict[str, object]] = []
    active: dict[tuple[str, str], str | None] = {}
    seen_versions: set[str] = set()
    for label in labels:
        key = (label.job_id, label.requirement)
        when = label.labeled_at.isoformat()
        if label.model_version and label.model_version not in seen_versions:
            if seen_versions:
                events.append(
                    {"type": "model_version", "at": when, "model_version": label.model_version}
                )
            seen_versions.add(label.model_version)
        verdict = label.corrected_status.value if label.corrected_status else None
        if label.action is LabelAction.WITHDRAW:
            kind = "withdrawn"
        elif key not in active:
            kind = "added"
        elif active[key] is None:
            kind = "restored"
        elif active[key] == verdict:
            kind = "confirmed"
        else:
            kind = "changed"
        events.append(
            {
                "type": kind,
                "at": when,
                "job_id": label.job_id,
                "requirement": label.requirement,
                "kind": label.kind,
                "from_status": active.get(key),
                "to_status": verdict,
                "predicted_status": label.predicted_status,
                "source": label.source.value,
                "sampled": label.sampled,
                "note": label.note,
                "model_version": label.model_version,
            }
        )
        active[key] = verdict
    return events


def daily_counts(events: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Return per-day event counts (UTC dates), newest day first."""
    days: dict[str, Counter[str]] = {}
    for event in events:
        day = str(event["at"])[:10]
        counts = days.setdefault(day, Counter())
        counts[str(event["type"])] += 1
        if event.get("sampled"):
            counts["sampled"] += 1
    kinds = ("added", "changed", "confirmed", "withdrawn", "restored", "sampled")
    return [
        {"date": day, **{kind: days[day].get(kind, 0) for kind in kinds}}
        for day in sorted(days, reverse=True)
    ]


def tracking_progress(statuses: Sequence[str], settings: LabelsConfig) -> dict[str, object]:
    """Count tracking decisions as preference labels against the simulator and B3 needs."""
    rules = settings.tracking
    positive = sum(status in rules.positive_statuses for status in statuses)
    negative = sum(status in rules.negative_statuses for status in statuses)
    total = positive + negative
    return {
        "positive": positive,
        "negative": negative,
        "total": total,
        "positive_statuses": list(rules.positive_statuses),
        "negative_statuses": list(rules.negative_statuses),
        "simulator": {
            "minimum": rules.simulator_minimum,
            "minimum_each": rules.simulator_minimum_each,
            "ready": total >= rules.simulator_minimum
            and positive >= rules.simulator_minimum_each
            and negative >= rules.simulator_minimum_each,
        },
        "b3": {
            "minimum": rules.b3_minimum,
            "target": rules.b3_target,
            "ready": total >= rules.b3_minimum,
        },
    }


def snapshot_dir(store: RequirementLabelStore) -> Path:
    """Return where snapshot manifests are kept, next to the label log."""
    return store.path.parent / SNAPSHOT_DIR


def _complete_lines(store: RequirementLabelStore) -> list[bytes]:
    """Return the log's complete lines (a torn final line is not part of any snapshot)."""
    if not store.path.exists():
        return []
    data = store.path.read_bytes()
    end = data.rfind(b"\n")
    return data[: end + 1].splitlines(keepends=True) if end >= 0 else []


def _prefix_hash(lines: Sequence[bytes], count: int) -> str:
    digest = hashlib.sha256()
    for line in lines[:count]:
        digest.update(line)
    return digest.hexdigest()


def create_snapshot(
    store: RequirementLabelStore,
    settings: LabelsConfig,
    comparison_model: Mapping[str, object] | None = None,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, object], bool]:
    """Freeze the current log as a snapshot; return its manifest and whether it is new.

    Saving twice without new labels returns the existing snapshot instead of a copy.
    """
    with store.lock:
        lines = _complete_lines(store)
        labels = store.all()
        digest = _prefix_hash(lines, len(lines))
        existing = next(
            (item for item in list_snapshots(store) if item["log_sha256"] == digest), None
        )
        if existing is not None:
            return existing, False
        created = now or datetime.now(UTC)
        newest: dict[tuple[str, str], RequirementLabel] = {}
        for label in labels:
            newest[(label.job_id, label.requirement)] = label
        active = [label for label in newest.values() if label.action is LabelAction.LABEL]
        manifest: dict[str, object] = {
            "snapshot_id": f"{created:%Y%m%d-%H%M%S}-{digest[:12]}",
            "created_at": created.isoformat(),
            "log_lines": len(lines),
            "log_sha256": digest,
            "active_labels": len(active),
            "withdrawn_lines": len(newest) - len(active),
            "sampled_labels": sum(label.sampled for label in active),
            "agree": sum(label.agrees is True for label in active),
            "by_source": dict(Counter(label.source.value for label in active)),
            "by_kind": dict(Counter(label.kind or "unknown" for label in active)),
            "by_corrected_status": dict(
                Counter(label.corrected_status.value for label in active if label.corrected_status)
            ),
            "label_model_versions": sorted(
                {label.model_version for label in active if label.model_version}
            ),
            "comparison_model_version": (comparison_model or {}).get("version"),
            "labels_config_version": settings.version,
            "queue_seed": settings.queue.seed,
        }
        folder = snapshot_dir(store)
        folder.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(folder / f"{manifest['snapshot_id']}.json", manifest)
    return manifest, True


def list_snapshots(store: RequirementLabelStore) -> list[dict[str, object]]:
    """Return saved snapshot manifests, newest first, each checked against the log."""
    folder = snapshot_dir(store)
    if not folder.is_dir():
        return []
    lines = _complete_lines(store)
    manifests: list[dict[str, object]] = []
    for path in sorted(folder.glob("*.json"), reverse=True):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict):
            continue
        count = manifest.get("log_lines")
        manifest["verified"] = (
            isinstance(count, int)
            and count <= len(lines)
            and _prefix_hash(lines, count) == manifest.get("log_sha256")
        )
        manifests.append(manifest)
    return manifests


def current_snapshot(store: RequirementLabelStore) -> dict[str, object] | None:
    """Return the verified snapshot that covers the whole log as it is now, if any.

    A number computed now can cite this id; with no match, save a snapshot first.
    """
    count = len(_complete_lines(store))
    return next(
        (
            item
            for item in list_snapshots(store)
            if item["verified"] is True and item.get("log_lines") == count
        ),
        None,
    )


def snapshot_labels(
    store: RequirementLabelStore, manifest: Mapping[str, object]
) -> list[RequirementLabel]:
    """Return the labels a verified snapshot froze, for reproducing a Phase 7 number."""
    count = manifest.get("log_lines")
    lines = _complete_lines(store)
    if not isinstance(count, int) or _prefix_hash(lines, count) != manifest.get("log_sha256"):
        raise ValueError("The label log no longer matches this snapshot")
    return [RequirementLabel.model_validate_json(line) for line in lines[:count] if line.strip()]


def build_changes(
    store: RequirementLabelStore,
    settings: LabelsConfig,
    tracking_statuses: Sequence[str],
) -> dict[str, object]:
    """Return the Changes view payload: daily counts, recent events, snapshots, tracking."""
    events = label_events(store.all())
    return {
        "days": daily_counts(events),
        "events": list(reversed(events))[: settings.recent_events],
        "event_count": len(events),
        "snapshots": list_snapshots(store),
        "tracking": tracking_progress(tracking_statuses, settings),
    }


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


__all__ = [
    "build_changes",
    "create_snapshot",
    "current_snapshot",
    "daily_counts",
    "label_events",
    "list_snapshots",
    "snapshot_dir",
    "snapshot_labels",
    "tracking_progress",
]
