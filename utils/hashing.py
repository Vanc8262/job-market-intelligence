"""Deterministic SHA-256 hashing utilities for identity and deduplication."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def sha256_text(value: str) -> str:
    """Return the SHA-256 hex digest for text."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_json_hash(payload: dict[str, Any]) -> str:
    """Return a deterministic SHA-256 hash for a JSON-serializable mapping."""
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256_text(serialized)


def generate_job_id(
    source: str, company: str, title: str, location: str | None, external_id: str | None
) -> str:
    """Generate a stable job identifier from source identity fields."""
    parts = [
        source.strip().lower(),
        company.strip().lower(),
        title.strip().lower(),
        location or "",
        external_id or "",
    ]
    return sha256_text("|".join(parts))


def content_hash(description: str | None, title: str, company: str) -> str:
    """Generate a content hash for change detection."""
    return sha256_text(
        "|".join([company.strip().lower(), title.strip().lower(), description or ""])
    )


def duplicate_detection_hash(company: str, title: str, location: str | None) -> str:
    """Generate a hash optimized for detecting duplicate postings across sources."""
    normalized_location = (location or "unknown").strip().lower()
    return sha256_text(
        "|".join([company.strip().lower(), title.strip().lower(), normalized_location])
    )
