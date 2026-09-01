"""DuckDB storage backend placeholder."""

from pathlib import Path

from storage.base import BaseStorage


class DuckDBStorage(BaseStorage):
    """Storage interface for DuckDB-backed datasets."""

    def save(self, key: str, payload: object) -> Path | None:
        """Persist payload to DuckDB."""
        raise NotImplementedError("DuckDB persistence is scheduled for a future phase.")

    def load(self, key: str) -> object:
        """Load payload from DuckDB."""
        raise NotImplementedError("DuckDB loading is scheduled for a future phase.")

    def exists(self, key: str) -> bool:
        """Return whether a DuckDB object exists."""
        raise NotImplementedError("DuckDB existence checks are scheduled for a future phase.")

    def delete(self, key: str) -> None:
        """Delete a DuckDB object."""
        raise NotImplementedError("DuckDB deletion is scheduled for a future phase.")
