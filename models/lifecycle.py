"""Typed posting lifecycle state and event models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class LifecycleStatus(StrEnum):
    """Observed availability state within a configured candidate profile."""

    ACTIVE = "active"
    MISSING = "missing"
    REMOVED = "removed"


class LifecycleEventType(StrEnum):
    """Material posting transitions recorded by lifecycle tracking."""

    DISCOVERED = "discovered"
    CHANGED = "changed"
    MISSING = "missing"
    REMOVED = "removed"
    REACTIVATED = "reactivated"


class LifecycleEntry(BaseModel):
    """Current lifecycle state for one target-specific source reference."""

    model_config = ConfigDict(extra="forbid")

    target_name: str
    source_name: str
    source_reference: str
    job_id: str
    external_id: str | None = None
    title: str
    employer: str
    profile_name: str
    query_names: list[str] = Field(default_factory=list)
    status: LifecycleStatus = LifecycleStatus.ACTIVE
    first_seen_at: datetime
    last_seen_at: datetime
    last_changed_at: datetime
    removed_at: datetime | None = None
    content_hash: str | None = None
    consecutive_missing_runs: int = Field(default=0, ge=0)
    observation_count: int = Field(default=1, ge=1)


class LifecycleState(BaseModel):
    """Persisted lifecycle ledger applied idempotently by collection run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_collection_run_id: str | None = None
    entries: dict[str, LifecycleEntry] = Field(default_factory=dict)


class LifecycleEvent(BaseModel):
    """One auditable transition detected during a lifecycle run."""

    model_config = ConfigDict(extra="forbid")

    event_type: LifecycleEventType
    detected_at: datetime
    collection_run_id: str
    target_name: str
    source_name: str
    source_reference: str
    job_id: str
    title: str
    employer: str
    previous_status: LifecycleStatus | None = None
    current_status: LifecycleStatus
    previous_content_hash: str | None = None
    current_content_hash: str | None = None


__all__ = [
    "LifecycleEntry",
    "LifecycleEvent",
    "LifecycleEventType",
    "LifecycleState",
    "LifecycleStatus",
]
