"""Parquet storage backend placeholder."""

from pathlib import Path

from storage.base import BaseStorage


class ParquetStorage(BaseStorage):
    """Storage interface for Parquet datasets."""

    def save(self, key: str, payload: object) -> Path | None:
        """Persist payload as Parquet."""
        raise NotImplementedError("Parquet persistence is scheduled for a future phase.")

    def load(self, key: str) -> object:
        """Load Parquet payload."""
        raise NotImplementedError("Parquet loading is scheduled for a future phase.")

    def exists(self, key: str) -> bool:
        """Return whether a Parquet payload exists."""
        raise NotImplementedError("Parquet existence checks are scheduled for a future phase.")

    def delete(self, key: str) -> None:
        """Delete a Parquet payload."""
        raise NotImplementedError("Parquet deletion is scheduled for a future phase.")
