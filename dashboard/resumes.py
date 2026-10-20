"""Dashboard payloads for the private resume review workflow."""

from __future__ import annotations

from typing import Any

from models.candidate import ResumeExtraction, ResumeVersion, ReviewSection
from profiles.service import ResumeIngestionService


def load_candidate_profile(service: ResumeIngestionService) -> dict[str, Any]:
    """Return the profile and its resume versions, without any resume text."""
    profile = service.profile()
    versions = []
    for version in service.repository.list_resume_versions(profile.profile_id):
        extractions = service.repository.list_extractions(version.resume_version_id)
        approved = next(
            (item for item in extractions if item.review_status.value == "approved"), None
        )
        versions.append(
            {
                **_version_summary(version, profile.active_resume_version_id),
                "latest_extraction_id": extractions[0].extraction_id if extractions else None,
                "approved": approved is not None,
            }
        )
    return {
        "profile": {
            "profile_id": profile.profile_id,
            "display_name": profile.display_name,
            "active_resume_version_id": profile.active_resume_version_id,
        },
        "resume_versions": versions,
    }


def load_resume_reviews(service: ResumeIngestionService) -> dict[str, Any]:
    """Return a summary row per extraction, newest first, without facts or text."""
    profile = service.profile()
    items: list[dict[str, Any]] = []
    for version in service.repository.list_resume_versions(profile.profile_id):
        for extraction in service.repository.list_extractions(version.resume_version_id):
            items.append(
                {
                    "resume_version": _version_summary(version, profile.active_resume_version_id),
                    **_extraction_summary(extraction),
                }
            )
    items.sort(key=lambda item: str(item["created_at"]), reverse=True)
    return {
        "items": items,
        "pending_count": sum(
            item["review_status"] == "pending" and item["can_review"] for item in items
        ),
        "approved_count": sum(item["review_status"] == "approved" for item in items),
    }


def load_resume_review(service: ResumeIngestionService, extraction_id: str) -> dict[str, Any]:
    """Return one extraction's facts plus its private extracted text for evidence checks."""
    profile = service.profile()
    extraction = service.repository.get_extraction(extraction_id)
    version = service.repository.get_resume_version(extraction.resume_version_id)
    if version.profile_id != profile.profile_id:
        raise LookupError(f"extraction not found: {extraction_id}")
    return {
        "resume_version": _version_summary(version, profile.active_resume_version_id),
        "extraction": {
            **extraction.model_dump(mode="json"),
            "can_review": extraction.can_review,
            "sections": [section.value for section in ReviewSection],
        },
        "text": service.extracted_text(version.resume_version_id, extraction.text_sha256),
    }


def _version_summary(version: ResumeVersion, active_id: str | None) -> dict[str, Any]:
    return {
        "resume_version_id": version.resume_version_id,
        "label": version.label,
        "media_type": version.media_type.value,
        "uploaded_at": version.uploaded_at.isoformat(),
        "is_active": version.resume_version_id == active_id,
    }


def _extraction_summary(extraction: ResumeExtraction) -> dict[str, Any]:
    return {
        "extraction_id": extraction.extraction_id,
        "status": extraction.status.value,
        "quality_score": extraction.quality_score,
        "review_status": extraction.review_status.value,
        "approved_sections": [section.value for section in extraction.approved_sections],
        "fact_counts": extraction.facts.section_counts(),
        "warning_count": len(extraction.warnings),
        "can_review": extraction.can_review,
        "created_at": extraction.created_at.isoformat(),
        "parser_version": extraction.parser_version,
    }


__all__ = ["load_candidate_profile", "load_resume_review", "load_resume_reviews"]
