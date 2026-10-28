"""Owner corrections of Phase 6.3 requirement verdicts, kept as labeled data.

Each time the owner agrees with a ✓/◐/✗ verdict, or says it should be another one, the
dashboard appends one JSON line to ``data/private/labels/requirement_labels.jsonl``. The
log is append-only: a later label for the same requirement supersedes the earlier one
when read, and a ``withdraw`` entry takes a line's label back, but nothing is rewritten,
so the full review history stays available for Phase 7 accuracy estimates. Lines hold
posting requirement text, verdicts, and the owner's optional note, never resume text.

Schema v2 adds where a label came from (``source``) and whether the line was drawn at
random by the review queue (``sampled``). Only sampled labels may feed an unbiased
accuracy estimate; drawer labels come from postings the owner chose to open. Both are
set by the server, never taken from the browser. Schema v1 lines read as drawer labels.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

LABELS_FILE = Path("labels") / "requirement_labels.jsonl"
MAX_REQUIREMENT_CHARS = 2000
MAX_NOTE_CHARS = 500


class RequirementVerdict(StrEnum):
    """A status the owner can assign to one requirement line."""

    MET = "met"
    PARTIAL = "partial"
    MISSING = "missing"


class LabelAction(StrEnum):
    """What one log entry does to its requirement line."""

    LABEL = "label"
    WITHDRAW = "withdraw"


class LabelSource(StrEnum):
    """Where a label was made."""

    DRAWER = "drawer"
    LABELS_TAB = "labels_tab"
    QUEUE = "queue"


# Sources a browser request may name. Queue labels are assigned by the server when it
# serves a sampled line (slice 3), so a request can never claim to be one.
CLIENT_SOURCES = frozenset({LabelSource.DRAWER, LabelSource.LABELS_TAB})


class RequirementLabel(BaseModel):
    """One owner judgment of one requirement line in one posting, or its withdrawal."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1, 2] = 2
    action: LabelAction = LabelAction.LABEL
    source: LabelSource = LabelSource.DRAWER
    sampled: bool = False
    sample_seed: int | None = None
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)
    job_id: str = Field(min_length=1, max_length=200)
    requirement: str = Field(min_length=1, max_length=MAX_REQUIREMENT_CHARS)
    predicted_status: str = Field(min_length=1, max_length=32)
    corrected_status: RequirementVerdict | None = None
    agrees: bool | None = None
    kind: str | None = None
    required: bool | None = None
    model_version: str | None = None
    settings_hash: str | None = None
    resume_version_id: str | None = None
    labeled_at: datetime

    @model_validator(mode="after")
    def _consistent(self) -> RequirementLabel:
        if self.action is LabelAction.LABEL and (
            self.corrected_status is None or self.agrees is None
        ):
            raise ValueError("a label needs corrected_status and agrees")
        if self.action is LabelAction.WITHDRAW and (
            self.corrected_status is not None or self.agrees is not None
        ):
            raise ValueError("a withdrawal carries no verdict")
        if self.sampled and self.source is not LabelSource.QUEUE:
            raise ValueError("only review-queue labels can be sampled")
        if self.schema_version == 1 and (
            self.action is not LabelAction.LABEL or self.source is not LabelSource.DRAWER
        ):
            raise ValueError("schema v1 labels are drawer labels")
        return self

    @property
    def withdrawn(self) -> bool:
        """True when this entry takes the line's label back."""
        return self.action is LabelAction.WITHDRAW


def labels_path(private_root: Path) -> Path:
    """Return where requirement labels are appended."""
    return private_root / LABELS_FILE


def find_requirement(
    comparisons: Mapping[str, object] | None, job_id: str, requirement: str
) -> dict[str, object] | None:
    """Return the stored comparison item for ``requirement`` in ``job_id``, if present."""
    jobs = comparisons.get("jobs") if comparisons else None
    detail = jobs.get(job_id) if isinstance(jobs, dict) else None
    dimensions = detail.get("dimensions") if isinstance(detail, dict) else None
    section = dimensions.get("requirements") if isinstance(dimensions, dict) else None
    items = section.get("items") if isinstance(section, dict) else None
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("text") == requirement:
            return item
    return None


class RequirementLabelStore:
    """Append owner labels to a private JSON Lines log and read the latest ones back."""

    def __init__(self, path: Path) -> None:
        """Configure the log location; the file is created on the first label."""
        self.path = path
        self._lock = RLock()

    @property
    def lock(self) -> RLock:
        """Re-entrant lock held while appending; hold it to check-then-append atomically."""
        return self._lock

    def record(
        self,
        comparisons: Mapping[str, object] | None,
        job_id: str,
        requirement: str,
        corrected_status: str,
        *,
        source: LabelSource = LabelSource.DRAWER,
        note: str | None = None,
        sample_seed: int | None = None,
        now: datetime | None = None,
    ) -> RequirementLabel:
        """Label one requirement from the stored comparison and append it to the log.

        ``sample_seed`` marks a review-queue label as randomly sampled; only the queue
        endpoint passes it, after checking the line is the one the sampler drew.

        The predicted status comes from the stored comparison, not from the request, so
        a label always pairs the owner's verdict with what the model actually said.
        """
        try:
            verdict = RequirementVerdict(corrected_status)
        except ValueError as exc:
            raise ValueError("corrected_status must be met, partial, or missing") from exc
        item = find_requirement(comparisons, job_id, requirement)
        if item is None:
            raise LookupError("This requirement is not in the latest comparison for the posting")
        model = comparisons.get("model") if comparisons else None
        resume = comparisons.get("resume") if comparisons else None
        model = model if isinstance(model, dict) else {}
        resume = resume if isinstance(resume, dict) else {}
        predicted = str(item.get("status"))
        required = item.get("required")
        label = RequirementLabel(
            source=source,
            sampled=sample_seed is not None,
            sample_seed=sample_seed,
            note=_clean_note(note),
            job_id=job_id,
            requirement=requirement,
            predicted_status=predicted,
            corrected_status=verdict,
            agrees=predicted == verdict.value,
            kind=_optional_str(item.get("kind")),
            required=required if isinstance(required, bool) else None,
            model_version=_optional_str(model.get("version")),
            settings_hash=_optional_str(model.get("settings_hash")),
            resume_version_id=_optional_str(resume.get("resume_version_id")),
            labeled_at=now or datetime.now(UTC),
        )
        self.append(label)
        return label

    def withdraw(
        self,
        job_id: str,
        requirement: str,
        *,
        source: LabelSource = LabelSource.LABELS_TAB,
        note: str | None = None,
        now: datetime | None = None,
    ) -> RequirementLabel:
        """Take back the active label on one line by appending a withdrawal.

        Works from the log rather than the comparison, so labels on lines that were
        reworded or postings that closed can be withdrawn too. The withdrawal repeats
        the label's prediction and model details so the history stays self-describing.
        """
        with self._lock:
            entries = self.history().get((job_id, requirement), [])
            if not entries or entries[-1].withdrawn:
                raise LookupError("There is no active label on this requirement to withdraw")
            previous = entries[-1]
            entry = RequirementLabel(
                action=LabelAction.WITHDRAW,
                source=source,
                note=_clean_note(note),
                job_id=job_id,
                requirement=requirement,
                predicted_status=previous.predicted_status,
                kind=previous.kind,
                required=previous.required,
                model_version=previous.model_version,
                settings_hash=previous.settings_hash,
                resume_version_id=previous.resume_version_id,
                labeled_at=now or datetime.now(UTC),
            )
            self.append(entry)
        return entry

    def append(self, label: RequirementLabel) -> None:
        """Write one label as a single durable JSON line."""
        line = label.model_dump_json() + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())

    def all(self) -> list[RequirementLabel]:
        """Return every label in write order.

        A torn final line (the process stopped mid-write) is skipped; any other invalid
        line is an error, because silently dropping labels would bias Phase 7 estimates.
        """
        with self._lock:
            if not self.path.exists():
                return []
            lines = self.path.read_text(encoding="utf-8").splitlines()
        labels: list[RequirementLabel] = []
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                labels.append(RequirementLabel.model_validate_json(line))
            except ValidationError as exc:
                if number == len(lines):
                    break
                raise ValueError(f"Invalid requirement label on line {number}") from exc
        return labels

    def history(self) -> dict[tuple[str, str], list[RequirementLabel]]:
        """Return every entry per (job id, requirement text), oldest first."""
        grouped: dict[tuple[str, str], list[RequirementLabel]] = {}
        for label in self.all():
            grouped.setdefault((label.job_id, label.requirement), []).append(label)
        return grouped

    def latest_for(self, job_id: str) -> dict[str, RequirementLabel]:
        """Return the newest active label per requirement text for one posting.

        A line whose newest entry is a withdrawal is left out, so it reads as unlabeled.
        """
        latest = {label.requirement: label for label in self.all() if label.job_id == job_id}
        return {text: label for text, label in latest.items() if not label.withdrawn}


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _clean_note(note: str | None) -> str | None:
    text = " ".join(note.split()) if note else ""
    return text or None


__all__ = [
    "CLIENT_SOURCES",
    "MAX_NOTE_CHARS",
    "LabelAction",
    "LabelSource",
    "RequirementLabel",
    "RequirementLabelStore",
    "RequirementVerdict",
    "find_requirement",
    "labels_path",
]
