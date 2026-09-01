"""Atomic local persistence for candidate application tracking state."""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from models.application_tracking import ApplicationRecord, ApplicationTrackingState

"""Class ApplicationTrackingRepository: Read and update a validated application-tracking JSON document."""
class ApplicationTrackingRepository:

    """__init__(self, path): Configure the repository with its local JSON document path."""
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()

    """load(self): Return validated state, or an empty state before the first write."""
    def load(self) -> ApplicationTrackingState:
        with self._lock:
            if not self.path.exists():
                return ApplicationTrackingState()
            return ApplicationTrackingState.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )

    """update(self, job_id: str, changes: Mapping[str, object]): Merge, validate, and atomically persist one opportunity record."""
    def update(self, job_id: str, changes: Mapping[str, object]) -> ApplicationRecord:
        with self._lock:
            state = self.load()
            existing = state.records.get(job_id)
            values: dict[str, object] = (
                dict(existing.model_dump()) if existing else {"job_id": job_id}
            )
            values.update(changes)

            now = datetime.now(UTC)
            values["job_id"] = job_id
            values["updated_at"] = now
            record = ApplicationRecord.model_validate(values)
            state.records[job_id] = record
            state.updated_at = now
            self._save(state)
            return record

    def _save(self, state: ApplicationTrackingState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary_path.write_text(
            json.dumps(state.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(self.path)
