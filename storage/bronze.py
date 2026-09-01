"""Bronze storage for raw source records."""

import json
from pathlib import Path

from storage.base import BaseStorage


class BronzeStorage(BaseStorage):
    """File-backed storage for immutable raw source payloads."""

    def save(self, key: str, payload: object) -> Path | None:
        """Persist a raw payload without overwriting an existing record."""
        path = self.path_for(key, ".json")
        if path.exists():
            raise FileExistsError(f"Bronze record already exists: {key}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True, default=str)
        return path

    def load(self, key: str) -> object:
        """Load a raw payload."""
        path = self.path_for(key, ".json")
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)

    def exists(self, key: str) -> bool:
        """Return whether a raw payload exists."""
        return self.path_for(key, ".json").exists()

    def delete(self, key: str) -> None:
        """Delete a raw payload."""
        path = self.path_for(key, ".json")
        if path.exists():
            path.unlink()
