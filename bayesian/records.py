"""Model cards: every Phase 8 output says which model, version, and settings made it."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel

from utils.hashing import stable_json_hash


def model_card(
    name: str, *, stage: str, settings: BaseModel, replaces: str, version: str
) -> dict[str, object]:
    """Return a JSON-safe description of the model that produced an output."""
    return {
        "name": name,
        "stage": stage,
        "version": version,
        "advisory": True,
        "replaces_or_extends": replaces,
        "settings_hash": stable_json_hash(settings.model_dump(mode="json"))[:16],
        "generated_at": datetime.now(UTC).isoformat(),
    }


__all__ = ["model_card"]
