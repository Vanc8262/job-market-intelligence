"""Owner corrections of Phase 6.3 requirement verdicts, kept as labeled data.

Each time the owner agrees with a ✓/◐/✗ verdict, or says it should be another one, the
dashboard appends one JSON line to ``data/private/labels/requirement_labels.jsonl``. The
log is append-only: a later label for the same requirement supersedes the earlier one
when read, but nothing is rewritten, so the full review history stays available for
Phase 7 accuracy estimates. Lines hold posting requirement text and verdicts, never
resume text.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

LABELS_FILE = Path("labels") / "requirement_labels.jsonl"
MAX_REQUIREMENT_CHARS = 2000


class RequirementVerdict(StrEnum):
    """A status the owner can assign to one requirement line."""

    MET = "met"
    PARTIAL = "partial"
    MISSING = "missing"


class RequirementLabel(BaseModel):
    """One owner judgment of one requirement line in one posting."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    job_id: str = Field(min_length=1, max_length=200)
    requirement: str = Field(min_length=1, max_length=MAX_REQUIREMENT_CHARS)
    predicted_status: str = Field(min_length=1, max_length=32)
    corrected_status: RequirementVerdict
    agrees: bool
    kind: str | None = None
    required: bool | None = None
    model_version: str | None = None
    settings_hash: str | None = None
    resume_version_id: str | None = None
    labeled_at: datetime


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

    def record(
        self,
        comparisons: Mapping[str, object] | None,
        job_id: str,
        requirement: str,
        corrected_status: str,
        *,
        now: datetime | None = None,
    ) -> RequirementLabel:
        """Label one requirement from the stored comparison and append it to the log.

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

    def latest_for(self, job_id: str) -> dict[str, RequirementLabel]:
        """Return the newest label per requirement text for one posting."""
        return {label.requirement: label for label in self.all() if label.job_id == job_id}


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


__all__ = [
    "RequirementLabel",
    "RequirementLabelStore",
    "RequirementVerdict",
    "find_requirement",
    "labels_path",
]
