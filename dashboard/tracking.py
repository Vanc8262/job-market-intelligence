"""Atomic local persistence for candidate application tracking state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from models.application_tracking import ApplicationRecord, ApplicationTrackingState
from profiles.repository import CandidateProfileRepository, ProfileRecordNotFoundError


class ApplicationTrackingRepository:
    """Read and update a validated application-tracking JSON document."""

    def __init__(
        self,
        path: Path,
        *,
        profile_repository: CandidateProfileRepository | None = None,
        profile_id: str = "profile-default",
    ) -> None:
        """Configure the repository with its local JSON document path."""
        self.path = path
        self.profile_repository = profile_repository
        self.profile_id = profile_id
        self._lock = RLock()

    def load(self) -> ApplicationTrackingState:
        """Return validated state, or an empty state before the first write."""
        with self._lock:
            if not self.path.exists():
                return ApplicationTrackingState(profile_id=self.profile_id)
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("application tracking state must be a JSON object")
            migrated = self._migrate_legacy_state(payload)
            state = ApplicationTrackingState.model_validate(migrated)
            if state.profile_id != self.profile_id:
                raise ValueError(
                    "application tracking state belongs to a different candidate profile"
                )
            if migrated is not payload:
                self._save(state)
            return state

    def update(self, job_id: str, changes: Mapping[str, object]) -> ApplicationRecord:
        """Merge, validate, and atomically persist one opportunity record."""
        with self._lock:
            state = self.load()
            existing = state.records.get(job_id)
            values: dict[str, object] = (
                dict(existing.model_dump()) if existing else {"job_id": job_id}
            )
            values.update(changes)

            resume_version_id = values.get("resume_version_id")
            if resume_version_id is not None:
                self._validate_resume_version(str(resume_version_id))

            now = datetime.now(UTC)
            values["job_id"] = job_id
            values["updated_at"] = now
            record = ApplicationRecord.model_validate(values)
            state.records[job_id] = record
            state.updated_at = now
            self._save(state)
            return record

    def _migrate_legacy_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Upgrade schema-v1 free-text resume references to typed identifiers."""
        if payload.get("schema_version", 1) != 1:
            return payload
        migrated = dict(payload)
        migrated["schema_version"] = 2
        migrated["profile_id"] = self.profile_id
        records = payload.get("records", {})
        if not isinstance(records, dict):
            return migrated
        migrated_records: dict[str, Any] = {}
        for job_id, raw_record in records.items():
            if not isinstance(raw_record, dict):
                migrated_records[str(job_id)] = raw_record
                continue
            record = dict(raw_record)
            legacy_reference = record.pop("resume_version", None)
            if isinstance(legacy_reference, str) and legacy_reference.strip():
                legacy_reference = legacy_reference.strip()
                resolved = self._resolve_legacy_resume_reference(legacy_reference)
                record["resume_version_id"] = resolved
                if resolved is None:
                    note = f"Legacy resume reference: {legacy_reference}"
                    existing_notes = str(record.get("notes") or "").strip()
                    record["notes"] = "\n".join(value for value in (existing_notes, note) if value)[
                        :5000
                    ]
            else:
                record["resume_version_id"] = None
            migrated_records[str(job_id)] = record
        migrated["records"] = migrated_records
        return migrated

    def _resolve_legacy_resume_reference(self, reference: str) -> str | None:
        """Resolve an old ID, label, or filename against stored resume metadata."""
        if self.profile_repository is None:
            return None
        normalized = reference.casefold()
        try:
            versions = self.profile_repository.list_resume_versions(self.profile_id)
        except ProfileRecordNotFoundError:
            return None
        for version in versions:
            candidates = (
                version.resume_version_id,
                version.version_label,
                version.original_filename,
            )
            if any(candidate.casefold() == normalized for candidate in candidates):
                return version.resume_version_id
        return None

    def _validate_resume_version(self, resume_version_id: str) -> None:
        """Require a selected resume version to belong to the active profile."""
        if self.profile_repository is None:
            return
        try:
            version = self.profile_repository.get_resume_version(resume_version_id)
        except ProfileRecordNotFoundError as exc:
            raise ValueError(f"resume version not found: {resume_version_id}") from exc
        if version.profile_id != self.profile_id:
            raise ValueError("resume version does not belong to the active candidate profile")

    def _save(self, state: ApplicationTrackingState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary_path.write_text(
            json.dumps(state.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(self.path)
