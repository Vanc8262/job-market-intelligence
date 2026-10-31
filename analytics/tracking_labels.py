"""Phase 8 B3 training data: an append-only log of your application-tracking decisions.

Tracking state (``data/application_tracking/state.json``) keeps only each posting's
current status, and the Gold mart is rebuilt every day, so neither can say what a posting
looked like when you decided on it. This log records every status change as a training
label, together with the relevance signals and 6.3 fit the dashboard showed at that
moment. B3 (relevance weights from your decisions) and the weight simulator's
calibration train on these rows.

Each decision is positive, negative, or neither, by the status lists in
``config/labels.yaml`` (``tracking``). Withdrawing a decision leaves its tracking status
alone and only keeps it out of training (a posting archived as a duplicate is not a
judgement of fit); restoring takes it back with the signals it was recorded with.
Decisions made before the log existed are added by a sync, with the signals of the day
they were added and ``source="sync"`` so a model can tell them apart.

Snapshots freeze the log the same way requirement-label snapshots do. The log stores
numbers and public posting facts only, never resume text.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from comparison.label_changes import (
    complete_lines,
    prefix_hash,
    read_manifests,
    tracking_progress,
    write_json_atomic,
)
from comparison.labels import LabelAction
from utils.config import LabelsConfig

TRACKING_LABELS_FILE = Path("labels") / "tracking_labels.jsonl"
TRACKING_SNAPSHOT_DIR = "tracking_snapshots"
# Relevance components whose signals (0 to 1) are kept as features.
SIGNALS = ("skills", "title", "experience", "location")
# Mart columns kept as features, as they were at the time of the decision.
FEATURE_COLUMNS = (
    "resume_relevance_score",
    "pre_cap_score",
    "match_band",
    "score_cap_reason",
    "title_tier",
    "title_match_type",
    "family_match",
    "home_region_match",
    "home_base_miles",
    "employment_preferred",
    "minimum_experience_years",
    "salary_midpoint",
)
POSTING_COLUMNS = ("title", "employer", "family", "seniority", "workplace_type")
# Status a posting has before you decide anything about it.
UNDECIDED = "new"

type FeatureValue = float | int | bool | str | None


class TrackingClass(StrEnum):
    """What a tracking decision says about your preference."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEITHER = "neither"


class TrackingSource(StrEnum):
    """Where a training-label entry came from."""

    TRACKING = "tracking"
    SYNC = "sync"
    LABELS_TAB = "labels_tab"


class TrackingLabel(BaseModel):
    """One logged tracking decision, or the withdrawal of one."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    action: LabelAction = LabelAction.LABEL
    source: TrackingSource
    job_id: str = Field(min_length=1, max_length=200)
    status: str = Field(min_length=1, max_length=40)
    label_class: TrackingClass
    features: dict[str, FeatureValue] = Field(default_factory=dict)
    posting: dict[str, str | None] = Field(default_factory=dict)
    # False when the posting was not in the current mart, so no signals were captured.
    captured: bool = False
    relevance_weights: dict[str, float] | None = None
    comparison_model_version: str | None = None
    resume_version_id: str | None = None
    labels_config_version: str | None = None
    note: str | None = Field(default=None, max_length=500)
    labeled_at: datetime

    @property
    def withdrawn(self) -> bool:
        """Return whether this entry takes the decision out of training."""
        return self.action is LabelAction.WITHDRAW


@dataclass(frozen=True)
class DecisionContext:
    """What the dashboard showed for each posting: mart rows, fit summaries, versions."""

    postings: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    fits: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    relevance_weights: dict[str, float] | None = None
    comparison_model_version: str | None = None
    resume_version_id: str | None = None


def tracking_labels_path(private_root: Path) -> Path:
    """Return the private training-label log for a private data root."""
    return private_root / TRACKING_LABELS_FILE


def tracking_class(status: str, settings: LabelsConfig) -> TrackingClass:
    """Return the training class of a tracking status."""
    if status in settings.tracking.positive_statuses:
        return TrackingClass.POSITIVE
    if status in settings.tracking.negative_statuses:
        return TrackingClass.NEGATIVE
    return TrackingClass.NEITHER


def fit_summary(detail: Mapping[str, object]) -> dict[str, object]:
    """Return a posting's 6.3 fit summary (scores only, no resume evidence)."""
    dimensions = detail.get("dimensions")
    dimensions = dimensions if isinstance(dimensions, dict) else {}
    requirements = dimensions.get("requirements")
    requirements = requirements if isinstance(requirements, dict) else {}
    keywords = dimensions.get("keywords")
    keywords = keywords if isinstance(keywords, dict) else {}
    return {
        "fit_score": detail.get("fit_score"),
        "band": detail.get("band"),
        "requirements_score": requirements.get("score"),
        "required_met": requirements.get("required_met"),
        "required_total": requirements.get("required_total"),
        "keywords_score": keywords.get("score"),
    }


def decision_context(
    mart: Mapping[str, object] | None, comparisons: Mapping[str, object] | None
) -> DecisionContext:
    """Build the context a decision is recorded with from the mart and 6.3 results."""
    rows = mart.get("opportunities") if mart else None
    postings = {
        str(row["job_id"]): row
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict) and row.get("job_id")
    }
    profile = mart.get("profile") if mart else None
    weights = profile.get("weights") if isinstance(profile, dict) else None
    jobs = comparisons.get("jobs") if comparisons else None
    fits = {
        str(job_id): fit_summary(detail)
        for job_id, detail in (jobs.items() if isinstance(jobs, dict) else [])
        if isinstance(detail, dict)
    }
    model = comparisons.get("model") if comparisons else None
    resume = comparisons.get("resume") if comparisons else None
    return DecisionContext(
        postings=postings,
        fits=fits,
        relevance_weights=(
            {str(name): float(value) for name, value in weights.items()}
            if isinstance(weights, dict)
            else None
        ),
        comparison_model_version=_text(model.get("version")) if isinstance(model, dict) else None,
        resume_version_id=(
            _text(resume.get("resume_version_id")) if isinstance(resume, dict) else None
        ),
    )


def decision_features(
    row: Mapping[str, object] | None, fit: Mapping[str, object] | None
) -> dict[str, FeatureValue]:
    """Return the scalar features of one posting as the dashboard showed it."""
    features: dict[str, FeatureValue] = {}
    if row is not None:
        for column in FEATURE_COLUMNS:
            features[column] = _scalar(row.get(column))
        components = row.get("score_components")
        components = components if isinstance(components, dict) else {}
        for name in SIGNALS:
            component = components.get(name)
            signal = component.get("signal") if isinstance(component, dict) else None
            features[f"{name}_signal"] = _scalar(signal)
    if fit is not None:
        features["fit_score"] = _scalar(fit.get("fit_score"))
        features["fit_band"] = _scalar(fit.get("band"))
        features["requirements_score"] = _scalar(fit.get("requirements_score"))
        features["keywords_score"] = _scalar(fit.get("keywords_score"))
    return features


class TrackingLabelStore:
    """Append tracking decisions to a private JSON Lines log and read them back."""

    def __init__(self, path: Path) -> None:
        """Configure the log location; the file is created on the first decision."""
        self.path = path
        self._lock = RLock()

    @property
    def lock(self) -> RLock:
        """Re-entrant lock held while appending; hold it to check-then-append atomically."""
        return self._lock

    def record(
        self,
        job_id: str,
        status: str,
        context: DecisionContext,
        settings: LabelsConfig,
        *,
        source: TrackingSource = TrackingSource.TRACKING,
        note: str | None = None,
        now: datetime | None = None,
    ) -> TrackingLabel:
        """Log one tracking decision with the posting's signals as they are now."""
        row = context.postings.get(job_id)
        label = TrackingLabel(
            source=source,
            job_id=job_id,
            status=status,
            label_class=tracking_class(status, settings),
            features=decision_features(row, context.fits.get(job_id)),
            posting={column: _text(row.get(column)) for column in POSTING_COLUMNS} if row else {},
            captured=row is not None,
            relevance_weights=context.relevance_weights,
            comparison_model_version=context.comparison_model_version,
            resume_version_id=context.resume_version_id,
            labels_config_version=settings.version,
            note=_clean_note(note),
            labeled_at=now or datetime.now(UTC),
        )
        self.append(label)
        return label

    def withdraw(
        self, job_id: str, *, note: str | None = None, now: datetime | None = None
    ) -> TrackingLabel:
        """Keep a posting's newest decision out of training by appending a withdrawal."""
        with self._lock:
            newest = self.latest().get(job_id)
            if newest is None or newest.withdrawn:
                raise LookupError("There is no active decision on this posting to withdraw")
            entry = newest.model_copy(
                update={
                    "action": LabelAction.WITHDRAW,
                    "source": TrackingSource.LABELS_TAB,
                    "note": _clean_note(note),
                    "labeled_at": now or datetime.now(UTC),
                }
            )
            self.append(entry)
        return entry

    def restore(
        self, job_id: str, *, note: str | None = None, now: datetime | None = None
    ) -> TrackingLabel:
        """Take a withdrawn decision back, with the signals it was first recorded with."""
        with self._lock:
            newest = self.latest().get(job_id)
            if newest is None or not newest.withdrawn:
                raise LookupError("This posting has no withdrawn decision to restore")
            entry = newest.model_copy(
                update={
                    "action": LabelAction.LABEL,
                    "source": TrackingSource.LABELS_TAB,
                    "note": _clean_note(note),
                    "labeled_at": now or datetime.now(UTC),
                }
            )
            self.append(entry)
        return entry

    def append(self, label: TrackingLabel) -> None:
        """Write one entry as a single durable JSON line."""
        line = label.model_dump_json() + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())

    def all(self) -> list[TrackingLabel]:
        """Return every entry in write order; a torn final line is skipped."""
        with self._lock:
            if not self.path.exists():
                return []
            lines = self.path.read_text(encoding="utf-8").splitlines()
        return _parse(lines)

    def latest(self) -> dict[str, TrackingLabel]:
        """Return the newest entry per posting, withdrawals included."""
        return {label.job_id: label for label in self.all()}


def record_change(
    store: TrackingLabelStore,
    job_id: str,
    previous_status: str | None,
    status: str,
    context: DecisionContext,
    settings: LabelsConfig,
) -> TrackingLabel | None:
    """Log a status change saved in the dashboard; edits that keep the status log nothing."""
    if status == previous_status:
        return None
    if previous_status is None and status == UNDECIDED:
        return None
    return store.record(job_id, status, context, settings)


def unlogged(labels: Sequence[TrackingLabel], statuses: Mapping[str, str]) -> dict[str, str]:
    """Return tracking statuses the log does not hold yet, by job id.

    A posting is unlogged when it has a decision but no log entry, or when its status
    changed since its newest entry (saved while the log was unavailable).
    """
    newest = {label.job_id: label for label in labels}
    missing: dict[str, str] = {}
    for job_id, status in statuses.items():
        entry = newest.get(job_id)
        if entry is None and status != UNDECIDED:
            missing[job_id] = status
        elif entry is not None and entry.status != status:
            missing[job_id] = status
    return missing


def sync(
    store: TrackingLabelStore,
    statuses: Mapping[str, str],
    context: DecisionContext,
    settings: LabelsConfig,
    *,
    now: datetime | None = None,
) -> list[TrackingLabel]:
    """Add every unlogged decision to the log, marked as a sync."""
    with store.lock:
        missing = unlogged(store.all(), statuses)
        return [
            store.record(job_id, status, context, settings, source=TrackingSource.SYNC, now=now)
            for job_id, status in sorted(missing.items())
        ]


def review_tracking(
    labels: Sequence[TrackingLabel],
    statuses: Mapping[str, str],
    postings: Mapping[str, Mapping[str, object]],
    settings: LabelsConfig,
) -> list[dict[str, object]]:
    """Return one row per logged posting, newest decision first.

    ``state`` is current while the posting is in the mart and closed after it leaves;
    ``in_sync`` is False when tracking now holds a different status than the log. The
    class comes from today's status lists, so editing them reclassifies old decisions.
    """
    history: dict[str, list[TrackingLabel]] = {}
    for label in labels:
        history.setdefault(label.job_id, []).append(label)
    rows: list[dict[str, object]] = []
    for job_id, entries in history.items():
        newest = entries[-1]
        posting = postings.get(job_id)
        rows.append(
            {
                "job_id": job_id,
                "title": _text(posting.get("title")) if posting else newest.posting.get("title"),
                "employer": (
                    _text(posting.get("employer")) if posting else newest.posting.get("employer")
                ),
                "status": newest.status,
                "label_class": tracking_class(newest.status, settings).value,
                "withdrawn": newest.withdrawn,
                "source": newest.source.value,
                "captured": newest.captured,
                "features": newest.features,
                "labeled_at": newest.labeled_at.isoformat(),
                "note": newest.note,
                "state": "current" if posting is not None else "closed",
                "tracking_status": statuses.get(job_id),
                "in_sync": statuses.get(job_id) == newest.status,
                "decisions": sum(not entry.withdrawn for entry in entries),
                "history": [
                    {
                        "action": entry.action.value,
                        "status": entry.status,
                        "label_class": tracking_class(entry.status, settings).value,
                        "source": entry.source.value,
                        "labeled_at": entry.labeled_at.isoformat(),
                        "note": entry.note,
                    }
                    for entry in entries
                ],
            }
        )
    rows.sort(key=lambda row: str(row["labeled_at"]), reverse=True)
    return rows


def summarize_tracking(
    rows: Sequence[Mapping[str, object]], missing: Mapping[str, str], settings: LabelsConfig
) -> dict[str, object]:
    """Count training labels and progress toward the simulator and B3 thresholds."""
    active = [row for row in rows if not row["withdrawn"]]
    classes = Counter(str(row["label_class"]) for row in active)
    return {
        "postings": len(rows),
        "positive": classes.get(TrackingClass.POSITIVE.value, 0),
        "negative": classes.get(TrackingClass.NEGATIVE.value, 0),
        "neither": classes.get(TrackingClass.NEITHER.value, 0),
        "withdrawn": len(rows) - len(active),
        "synced": sum(row["source"] == TrackingSource.SYNC.value for row in active),
        "closed": sum(row["state"] == "closed" for row in active),
        "unlogged": len(missing),
        "progress": tracking_progress([str(row["status"]) for row in active], settings),
    }


def training_rows(
    labels: Sequence[TrackingLabel], settings: LabelsConfig
) -> list[dict[str, object]]:
    """Return B3 training rows: the newest active positive or negative decision per posting."""
    newest = {label.job_id: label for label in labels}
    rows: list[dict[str, object]] = []
    for label in newest.values():
        label_class = tracking_class(label.status, settings)
        if label.withdrawn or label_class is TrackingClass.NEITHER:
            continue
        rows.append(
            {
                "job_id": label.job_id,
                "label": 1 if label_class is TrackingClass.POSITIVE else 0,
                "status": label.status,
                "source": label.source.value,
                "captured": label.captured,
                "labeled_at": label.labeled_at.isoformat(),
                "relevance_weights": label.relevance_weights,
                **label.features,
            }
        )
    return rows


def tracking_snapshot_dir(store: TrackingLabelStore) -> Path:
    """Return where training-label snapshot manifests are kept."""
    return store.path.parent / TRACKING_SNAPSHOT_DIR


def create_tracking_snapshot(
    store: TrackingLabelStore, settings: LabelsConfig, *, now: datetime | None = None
) -> tuple[dict[str, object], bool]:
    """Freeze the training log as a snapshot; return its manifest and whether it is new."""
    with store.lock:
        lines = complete_lines(store.path)
        digest = prefix_hash(lines, len(lines))
        existing = next(
            (item for item in list_tracking_snapshots(store) if item["log_sha256"] == digest),
            None,
        )
        if existing is not None:
            return existing, False
        labels = _parse([line.decode("utf-8") for line in lines])
        newest = {label.job_id: label for label in labels}
        active = [label for label in newest.values() if not label.withdrawn]
        created = now or datetime.now(UTC)
        classes = Counter(tracking_class(label.status, settings).value for label in active)
        manifest: dict[str, object] = {
            "snapshot_id": f"{created:%Y%m%d-%H%M%S}-{digest[:12]}",
            "created_at": created.isoformat(),
            "log_lines": len(lines),
            "log_sha256": digest,
            "active_decisions": len(active),
            "positive": classes.get(TrackingClass.POSITIVE.value, 0),
            "negative": classes.get(TrackingClass.NEGATIVE.value, 0),
            "neither": classes.get(TrackingClass.NEITHER.value, 0),
            "withdrawn": len(newest) - len(active),
            "synced": sum(label.source is TrackingSource.SYNC for label in active),
            "by_status": dict(Counter(label.status for label in active)),
            "labels_config_version": settings.version,
        }
        folder = tracking_snapshot_dir(store)
        folder.mkdir(parents=True, exist_ok=True)
        write_json_atomic(folder / f"{manifest['snapshot_id']}.json", manifest)
    return manifest, True


def list_tracking_snapshots(store: TrackingLabelStore) -> list[dict[str, object]]:
    """Return saved training-label snapshots, newest first, each checked against the log."""
    return read_manifests(tracking_snapshot_dir(store), complete_lines(store.path))


def tracking_snapshot_labels(
    store: TrackingLabelStore, manifest: Mapping[str, object]
) -> list[TrackingLabel]:
    """Return the entries a verified snapshot froze, for reproducing a B3 fit."""
    count = manifest.get("log_lines")
    lines = complete_lines(store.path)
    if not isinstance(count, int) or prefix_hash(lines, count) != manifest.get("log_sha256"):
        raise ValueError("The training-label log no longer matches this snapshot")
    return _parse([line.decode("utf-8") for line in lines[:count]])


def _parse(lines: Sequence[str]) -> list[TrackingLabel]:
    labels: list[TrackingLabel] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            labels.append(TrackingLabel.model_validate_json(line))
        except ValidationError as exc:
            if number == len(lines):
                break
            raise ValueError(f"Invalid training label on line {number}") from exc
    return labels


def _scalar(value: object) -> FeatureValue:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _clean_note(note: str | None) -> str | None:
    text = " ".join(note.split()) if note else ""
    return text[:500] or None


__all__ = [
    "DecisionContext",
    "TrackingClass",
    "TrackingLabel",
    "TrackingLabelStore",
    "TrackingSource",
    "create_tracking_snapshot",
    "decision_context",
    "decision_features",
    "fit_summary",
    "list_tracking_snapshots",
    "record_change",
    "review_tracking",
    "summarize_tracking",
    "sync",
    "tracking_class",
    "tracking_labels_path",
    "tracking_snapshot_labels",
    "training_rows",
    "unlogged",
]
