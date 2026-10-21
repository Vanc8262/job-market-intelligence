"""Build the scoring profile from the approved active resume plus YAML preferences.

Resume content (skills and years of experience) comes from the active resume version's
approved, not superseded extraction. Preferences a resume does not state (target
titles, home bases, employment types, weights) always come from
``config/resume_profile.yaml``, whose skill list is the fallback when no approved
resume exists or ``resume_source`` is ``config``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from models.candidate import ExperienceFact, ResumeExtraction
from profiles.repository import CandidateProfileRepository, ProfileRecordNotFoundError
from utils.config import Config, ResumeProfileConfig

DATABASE_NAME = "candidates.sqlite3"


def experience_years(experience: list[ExperienceFact], *, today: datetime | None = None) -> float:
    """Return total years of experience, counting overlapping roles once.

    A missing start month counts from January, a missing end month through December,
    and a current role runs to today. Roles without a start date are skipped.
    """
    now = today or datetime.now(UTC)
    spans: list[tuple[int, int]] = []
    for role in experience:
        if role.start is None:
            continue
        start = role.start.year * 12 + (role.start.month or 1) - 1
        if role.current or role.end is None:
            end = now.year * 12 + now.month if role.current else start + 1
        else:
            end = role.end.year * 12 + (role.end.month or 12)
        if end > start:
            spans.append((start, end))
    months = 0
    current_start, current_end = None, None
    for start, end in sorted(spans):
        if current_end is None or start > current_end:
            if current_end is not None and current_start is not None:
                months += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_end is not None and current_start is not None:
        months += current_end - current_start
    return round(months / 12, 2)


def approved_resume_extraction(
    config: Config, *, private_root: Path | None = None
) -> ResumeExtraction | None:
    """Return the active resume's approved extraction without creating any files."""
    root = Path(private_root or config.settings.paths.private_dir)
    database = root / DATABASE_NAME
    if not database.is_file():
        return None
    repository = CandidateProfileRepository(database)
    try:
        profile = repository.get_profile(config.settings.candidate_profile.default_profile_id)
    except ProfileRecordNotFoundError:
        return None
    if profile.active_resume_version_id is None:
        return None
    return repository.latest_approved_extraction(profile.active_resume_version_id)


def resolve_resume_profile(
    config: Config, *, private_root: Path | None = None
) -> tuple[ResumeProfileConfig, dict[str, object]]:
    """Return the scoring profile and a JSON-safe note of where its resume facts came from."""
    base = config.resume_profile
    if base.resume_source == "config":
        return base, {"source": "config", "reason": "resume_source is config"}
    extraction = approved_resume_extraction(config, private_root=private_root)
    if extraction is None:
        return base, {"source": "config", "reason": "no approved active resume"}
    skills = list(
        dict.fromkeys(
            (fact.canonical or fact.name).strip()
            for fact in extraction.facts.skills
            if (fact.canonical or fact.name).strip()
        )
    )
    if not skills:
        return base, {"source": "config", "reason": "approved resume lists no skills"}
    present = {skill.casefold() for skill in skills}
    years = experience_years(extraction.facts.experience)
    scoring = base.scoring.model_copy(
        update={
            "core_skills": [
                skill for skill in base.scoring.core_skills if skill.casefold() in present
            ],
            # Allow postings that ask for up to one year more than the resume shows.
            "maximum_preferred_experience_years": float(round(years) + 1),
        }
    )
    profile = ResumeProfileConfig.model_validate(
        {**base.model_dump(), "skills": skills, "scoring": scoring.model_dump()}
    )
    return profile, {
        "source": "approved_resume",
        "resume_version_id": extraction.resume_version_id,
        "extraction_id": extraction.extraction_id,
        "approved_at": extraction.approved_at.isoformat() if extraction.approved_at else None,
        "skill_count": len(skills),
        "experience_years": years,
    }


__all__ = ["approved_resume_extraction", "experience_years", "resolve_resume_profile"]
