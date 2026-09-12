"""Typed candidate workflow state for shortlisted and applied opportunities."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ApplicationStatus(StrEnum):
    """Candidate-controlled progress for one opportunity."""

    NEW = "new"
    REVIEWING = "reviewing"
    SAVED = "saved"
    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class ApplicationPriority(StrEnum):
    """Candidate-assigned attention level for an opportunity."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ApplicationRecord(BaseModel):
    """Mutable candidate workflow state keyed by canonical job ID."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1)
    shortlisted: bool = False
    status: ApplicationStatus = ApplicationStatus.NEW
    priority: ApplicationPriority = ApplicationPriority.NONE
    application_date: date | None = None
    follow_up_date: date | None = None
    resume_version_id: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str = Field(default="", max_length=5000)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ApplicationTrackingState(BaseModel):
    """Versioned local writeback store for all candidate workflow records."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    profile_id: str = Field(default="profile-default", min_length=1)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    records: dict[str, ApplicationRecord] = Field(default_factory=dict)


__all__ = [
    "ApplicationPriority",
    "ApplicationRecord",
    "ApplicationStatus",
    "ApplicationTrackingState",
]
