"""Shared storage protocol and base functionality."""

from abc import ABC, abstractmethod
from pathlib import Path


class BaseStorage(ABC):
    """Consistent interface for all storage backends."""

    def __init__(self, root: str | Path) -> None:
        """Initialize a storage backend rooted at a path."""
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str, suffix: str) -> Path:
        """Return a safe file path for a logical storage key."""
        clean_suffix = suffix if suffix.startswith(".") else f".{suffix}"
        safe_parts = [self._safe_part(part) for part in Path(key).parts if part not in {"", "."}]
        if not safe_parts:
            raise ValueError("storage key must not be empty")
        relative_path = Path(*safe_parts).with_suffix(clean_suffix)
        path = (self.root / relative_path).resolve()
        if self.root != path and self.root not in path.parents:
            raise ValueError(f"storage key escapes root: {key}")
        return path

    @staticmethod
    def _safe_part(part: str) -> str:
        """Normalize one path segment for portable local storage."""
        if part in {"..", ""}:
            raise ValueError(f"invalid storage key segment: {part!r}")
        return "".join(
            character if character.isalnum() or character in "-_." else "_" for character in part
        )

    @abstractmethod
    def save(self, key: str, payload: object) -> Path | None:
        """Persist a payload under a key."""
        raise NotImplementedError

    @abstractmethod
    def load(self, key: str) -> object:
        """Load a payload by key."""
        raise NotImplementedError

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Return whether a key exists."""
        raise NotImplementedError

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete a payload by key."""
        raise NotImplementedError
