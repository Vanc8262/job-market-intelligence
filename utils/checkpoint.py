"""Checkpoint utilities for resumable extraction and processing workflows."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Checkpoint:
    """Serializable checkpoint state for a resumable job."""

    name: str
    position: str | int | None = None
    state: dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class CheckpointManager:
    """Manage JSON checkpoints for source extractors and processing jobs."""

    def __init__(self, checkpoint_dir: str | Path) -> None:
        """Create a checkpoint manager rooted at a directory."""
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    def save(self, checkpoint: Checkpoint) -> Path:
        """Persist a checkpoint and return the written path."""
        path = self._path_for(checkpoint.name)
        payload = asdict(
            Checkpoint(
                name=checkpoint.name,
                position=checkpoint.position,
                state=checkpoint.state,
                updated_at=datetime.now(UTC).isoformat(),
            )
        )
        with path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True)
        return path

    def load(self, name: str) -> Checkpoint | None:
        """Load a checkpoint by name if it exists."""
        path = self._path_for(name)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        return Checkpoint(**payload)

    def clear(self, name: str) -> None:
        """Delete a checkpoint if it exists."""
        path = self._path_for(name)
        if path.exists():
            path.unlink()

    def exists(self, name: str) -> bool:
        """Return whether a checkpoint exists."""
        return self._path_for(name).exists()

    def _path_for(self, name: str) -> Path:
        """Return the JSON checkpoint path for a checkpoint name."""
        safe_name = "".join(
            character if character.isalnum() or character in "-_" else "_" for character in name
        )
        return self.checkpoint_dir / f"{safe_name}.json"
