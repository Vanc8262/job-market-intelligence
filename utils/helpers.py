"""General-purpose helper utilities used across JMIP."""

from pathlib import Path
from typing import Any


def ensure_directory(path: str | Path) -> Path:
    """Create a directory if needed and return it as a Path."""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def remove_none_values(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy without keys whose values are None."""
    return {key: value for key, value in payload.items() if value is not None}
