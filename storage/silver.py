"""Silver storage for validated canonical job postings."""

import json
from pathlib import Path

from models.job_posting import JobPosting
from storage.base import BaseStorage


class SilverStorage(BaseStorage):
    """File-backed storage for validated canonical job postings."""

    def save(self, key: str, payload: object) -> Path | None:
        """Persist a canonical payload."""
        posting = self._coerce_posting(payload)
        path = self.path_for(key, ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as file:
            json.dump(posting.model_dump(mode="json"), file, indent=2, sort_keys=True)
        return path

    def load(self, key: str) -> JobPosting:
        """Load a canonical payload."""
        path = self.path_for(key, ".json")
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        return JobPosting.model_validate(payload)

    def exists(self, key: str) -> bool:
        """Return whether a canonical payload exists."""
        return self.path_for(key, ".json").exists()

    def delete(self, key: str) -> None:
        """Delete a canonical payload."""
        path = self.path_for(key, ".json")
        if path.exists():
            path.unlink()

    @staticmethod
    def _coerce_posting(payload: object) -> JobPosting:
        """Return a validated canonical posting from a model or mapping."""
        if isinstance(payload, JobPosting):
            return payload
        return JobPosting.model_validate(payload)
