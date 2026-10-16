"""SQLite store for candidate profiles, resume versions, extractions, and reviews.

Each row keeps the validated Pydantic document as JSON next to the few columns used for
lookups. Review writes are guarded by a revision number so a stale browser tab cannot
overwrite a newer review.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from models.candidate import (
    CandidateProfile,
    ResumeExtraction,
    ResumeVersion,
    ReviewApproval,
    ReviewDraft,
    ReviewStatus,
)

SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS profiles (
    profile_id TEXT PRIMARY KEY,
    document TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resume_versions (
    resume_version_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
    sha256 TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    document TEXT NOT NULL,
    UNIQUE (profile_id, sha256)
);
CREATE TABLE IF NOT EXISTS extractions (
    extraction_id TEXT PRIMARY KEY,
    resume_version_id TEXT NOT NULL REFERENCES resume_versions(resume_version_id),
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    document TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS extractions_by_version ON extractions(resume_version_id, created_at);
"""


class ProfileRepositoryError(RuntimeError):
    """Base error for candidate profile storage."""


class ProfileRecordNotFoundError(ProfileRepositoryError):
    """Raised when a requested profile, resume version, or extraction does not exist."""


class ProfileRepositoryConflictError(ProfileRepositoryError):
    """Raised when a write is based on stale data or breaks a review rule."""


class CandidateProfileRepository:
    """Transactional access to private candidate metadata."""

    def __init__(self, path: str | Path) -> None:
        """Open (and create when needed) the SQLite database at ``path``."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            row = connection.execute("SELECT version FROM schema_info").fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif row[0] != SCHEMA_VERSION:
                raise ProfileRepositoryError(
                    f"profile database schema {row[0]} is not supported (expected {SCHEMA_VERSION})"
                )

    # Profiles ---------------------------------------------------------------------

    def ensure_profile(self, profile_id: str, display_name: str) -> CandidateProfile:
        """Return the profile, creating it on first use."""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT document FROM profiles WHERE profile_id = ?", (profile_id,)
            ).fetchone()
            if row is not None:
                return CandidateProfile.model_validate_json(row[0])
            profile = CandidateProfile(profile_id=profile_id, display_name=display_name)
            connection.execute(
                "INSERT INTO profiles (profile_id, document) VALUES (?, ?)",
                (profile.profile_id, profile.model_dump_json()),
            )
            return profile

    def get_profile(self, profile_id: str) -> CandidateProfile:
        """Return one profile or raise ``ProfileRecordNotFoundError``."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document FROM profiles WHERE profile_id = ?", (profile_id,)
            ).fetchone()
        if row is None:
            raise ProfileRecordNotFoundError(f"profile not found: {profile_id}")
        return CandidateProfile.model_validate_json(row[0])

    def set_active_resume(self, profile_id: str, resume_version_id: str | None) -> CandidateProfile:
        """Make one of the profile's resume versions active (or clear it with None)."""
        with self._lock, self._connect() as connection:
            profile = self.get_profile(profile_id)
            if resume_version_id is not None:
                version = self.get_resume_version(resume_version_id)
                if version.profile_id != profile_id:
                    raise ProfileRepositoryConflictError(
                        "resume version belongs to another profile"
                    )
            profile.active_resume_version_id = resume_version_id
            profile.updated_at = _now()
            connection.execute(
                "UPDATE profiles SET document = ? WHERE profile_id = ?",
                (profile.model_dump_json(), profile_id),
            )
            return profile

    # Resume versions ----------------------------------------------------------------

    def add_resume_with_extraction(
        self, version: ResumeVersion, extraction: ResumeExtraction
    ) -> None:
        """Store a new resume version and its first extraction in one transaction."""
        if extraction.resume_version_id != version.resume_version_id:
            raise ValueError("extraction does not belong to this resume version")
        with self._lock, self._connect() as connection:
            self.get_profile(version.profile_id)
            try:
                connection.execute(
                    "INSERT INTO resume_versions "
                    "(resume_version_id, profile_id, sha256, uploaded_at, document) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        version.resume_version_id,
                        version.profile_id,
                        version.sha256,
                        version.uploaded_at.isoformat(),
                        version.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ProfileRepositoryConflictError(
                    "this exact file is already stored for the profile"
                ) from exc
            self._insert_extraction(connection, extraction)

    def get_resume_version(self, resume_version_id: str) -> ResumeVersion:
        """Return one resume version or raise ``ProfileRecordNotFoundError``."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document FROM resume_versions WHERE resume_version_id = ?",
                (resume_version_id,),
            ).fetchone()
        if row is None:
            raise ProfileRecordNotFoundError(f"resume version not found: {resume_version_id}")
        return ResumeVersion.model_validate_json(row[0])

    def find_resume_by_sha256(self, profile_id: str, sha256: str) -> ResumeVersion | None:
        """Return the profile's resume version with this file hash, if any."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document FROM resume_versions WHERE profile_id = ? AND sha256 = ?",
                (profile_id, sha256),
            ).fetchone()
        return None if row is None else ResumeVersion.model_validate_json(row[0])

    def list_resume_versions(self, profile_id: str) -> list[ResumeVersion]:
        """Return the profile's resume versions, newest first."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT document FROM resume_versions WHERE profile_id = ? "
                "ORDER BY uploaded_at DESC, resume_version_id",
                (profile_id,),
            ).fetchall()
        return [ResumeVersion.model_validate_json(row[0]) for row in rows]

    # Extractions --------------------------------------------------------------------

    def add_extraction(self, extraction: ResumeExtraction) -> None:
        """Store an additional extraction (for example after a parser upgrade)."""
        with self._lock, self._connect() as connection:
            self.get_resume_version(extraction.resume_version_id)
            self._insert_extraction(connection, extraction)

    def get_extraction(self, extraction_id: str) -> ResumeExtraction:
        """Return one extraction or raise ``ProfileRecordNotFoundError``."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document FROM extractions WHERE extraction_id = ?", (extraction_id,)
            ).fetchone()
        if row is None:
            raise ProfileRecordNotFoundError(f"extraction not found: {extraction_id}")
        return ResumeExtraction.model_validate_json(row[0])

    def list_extractions(self, resume_version_id: str) -> list[ResumeExtraction]:
        """Return a resume version's extractions, newest first."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT document FROM extractions WHERE resume_version_id = ? "
                "ORDER BY created_at DESC, extraction_id",
                (resume_version_id,),
            ).fetchall()
        return [ResumeExtraction.model_validate_json(row[0]) for row in rows]

    def latest_approved_extraction(self, resume_version_id: str) -> ResumeExtraction | None:
        """Return the newest approved extraction of a resume version, if any."""
        return next(
            (
                extraction
                for extraction in self.list_extractions(resume_version_id)
                if extraction.review_status == ReviewStatus.APPROVED
            ),
            None,
        )

    # Review -------------------------------------------------------------------------

    def save_review_draft(
        self, extraction_id: str, draft: ReviewDraft, *, profile_id: str
    ) -> ResumeExtraction:
        """Save corrected facts and per-section approvals without finalizing them."""
        return self._write_review(extraction_id, draft, profile_id=profile_id, approve=False)

    def approve_review(
        self, extraction_id: str, approval: ReviewApproval, *, profile_id: str
    ) -> ResumeExtraction:
        """Finalize a review once every section is approved; it then becomes read-only."""
        return self._write_review(extraction_id, approval, profile_id=profile_id, approve=True)

    def _write_review(
        self, extraction_id: str, draft: ReviewDraft, *, profile_id: str, approve: bool
    ) -> ResumeExtraction:
        with self._lock, self._connect() as connection:
            extraction = self.get_extraction(extraction_id)
            version = self.get_resume_version(extraction.resume_version_id)
            if version.profile_id != profile_id:
                raise ProfileRecordNotFoundError(f"extraction not found: {extraction_id}")
            if not extraction.can_review:
                raise ProfileRepositoryConflictError(
                    "this extraction is already approved or failed quality checks"
                )
            if draft.revision != extraction.revision:
                raise ProfileRepositoryConflictError(
                    f"review is out of date (revision {draft.revision}, current "
                    f"{extraction.revision}); reload before saving"
                )
            now = _now()
            updated = extraction.model_copy(
                update={
                    "facts": draft.facts,
                    "approved_sections": draft.approved_sections,
                    "review_comment": draft.review_comment,
                    "revision": extraction.revision + 1,
                    "review_updated_at": now,
                    "review_status": ReviewStatus.APPROVED if approve else ReviewStatus.PENDING,
                    "approved_at": now if approve else None,
                }
            )
            updated = ResumeExtraction.model_validate(updated.model_dump())
            cursor = connection.execute(
                "UPDATE extractions SET document = ?, revision = ? "
                "WHERE extraction_id = ? AND revision = ?",
                (updated.model_dump_json(), updated.revision, extraction_id, extraction.revision),
            )
            if cursor.rowcount != 1:
                raise ProfileRepositoryConflictError(
                    "review changed while saving; reload and retry"
                )
            return updated

    # Internals ----------------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _insert_extraction(connection: sqlite3.Connection, extraction: ResumeExtraction) -> None:
        connection.execute(
            "INSERT INTO extractions "
            "(extraction_id, resume_version_id, revision, created_at, document) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                extraction.extraction_id,
                extraction.resume_version_id,
                extraction.revision,
                extraction.created_at.isoformat(),
                extraction.model_dump_json(),
            ),
        )


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "CandidateProfileRepository",
    "ProfileRecordNotFoundError",
    "ProfileRepositoryConflictError",
    "ProfileRepositoryError",
]
