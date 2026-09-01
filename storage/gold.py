"""Gold storage for analytics-ready datasets."""

import json
from pathlib import Path
from typing import Any, cast

from storage.base import BaseStorage


class GoldStorage(BaseStorage):
    """File-backed storage for analytics-ready outputs."""

    def save(self, key: str, payload: object) -> Path | None:
        """Persist an analytics-ready payload."""
        normalized_payload = self._normalize_payload(payload)
        path = self.path_for(key, ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as file:
            json.dump(normalized_payload, file, indent=2, sort_keys=True, default=str)
        return path

    def load(self, key: str) -> object:
        """Load an analytics-ready payload."""
        path = self.path_for(key, ".json")
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)

    def exists(self, key: str) -> bool:
        """Return whether an analytics-ready payload exists."""
        return self.path_for(key, ".json").exists()

    def delete(self, key: str) -> None:
        """Delete an analytics-ready payload."""
        path = self.path_for(key, ".json")
        if path.exists():
            path.unlink()

    @staticmethod
    def _normalize_payload(payload: object) -> object:
        """Convert common analytics objects into JSON-serializable records."""
        if hasattr(payload, "to_dict"):
            try:
                return cast(Any, payload).to_dict(orient="records")
            except TypeError:
                return cast(Any, payload).to_dict()
        if hasattr(payload, "to_dicts"):
            return cast(Any, payload).to_dicts()
        return payload
