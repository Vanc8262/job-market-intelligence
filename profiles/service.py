"""Resume ingestion: store the original privately, extract, score, parse, and record."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from models.candidate import (
    CandidateProfile,
    ExtractionStatus,
    ResumeExtraction,
    ResumeMediaType,
    ResumeVersion,
    ReviewStatus,
)
from profiles.documents import (
    DocumentText,
    ResumeDocumentError,
    extract_document_text,
    normalize_text,
)
from profiles.parser import PARSER_NAME, PARSER_VERSION, ResumeParser, SkillVocabulary
from profiles.quality import assess_text_quality
from profiles.repository import (
    CandidateProfileRepository,
    ProfileRecordNotFoundError,
    ProfileRepositoryConflictError,
)
from utils.config import Config
from utils.hashing import sha256_text

DATABASE_NAME = "candidates.sqlite3"


@dataclass(frozen=True)
class IngestionResult:
    """Outcome of one ingestion request."""

    resume_version: ResumeVersion
    extraction: ResumeExtraction | None
    duplicate: bool
    activated: bool
    message: str

    def summary(self) -> dict[str, object]:
        """Return a JSON-safe summary without resume text."""
        extraction = self.extraction
        return {
            "resume_version_id": self.resume_version.resume_version_id,
            "label": self.resume_version.label,
            "media_type": self.resume_version.media_type.value,
            "duplicate": self.duplicate,
            "activated": self.activated,
            "message": self.message,
            "extraction": None
            if extraction is None
            else {
                "extraction_id": extraction.extraction_id,
                "status": extraction.status.value,
                "quality_score": extraction.quality_score,
                "fact_counts": extraction.facts.section_counts(),
                "warnings": extraction.warnings,
            },
        }


LATEST_TEXT = "extracted.txt"


class ResumeIngestionService:
    """Ingest PDF and DOCX resumes into the private candidate store."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        repository: CandidateProfileRepository | None = None,
        private_root: Path | None = None,
    ) -> None:
        """Wire configuration, the private data folder, and the metadata repository."""
        self.config = config or Config()
        settings = self.config.settings
        self.private_root = Path(private_root or settings.paths.private_dir)
        self.repository = repository or CandidateProfileRepository(
            self.private_root / DATABASE_NAME
        )
        self.limits = settings.resume_ingestion
        self.profile_id = settings.candidate_profile.default_profile_id
        self.parser = ResumeParser(
            SkillVocabulary(self.config.skills, self.config.resume_profile.skills)
        )

    def profile(self) -> CandidateProfile:
        """Return the default profile, creating it on first use."""
        return self.repository.ensure_profile(
            self.profile_id, self.config.resume_profile.profile_name
        )

    def ingest(
        self, source: Path, *, label: str | None = None, activate: bool = False
    ) -> IngestionResult:
        """Ingest one file; an identical file already stored is reported, not duplicated."""
        profile = self.profile()
        size = source.stat().st_size
        if size > self.limits.max_file_size_bytes:
            raise ResumeDocumentError(
                f"file is {size:,} bytes; the limit is {self.limits.max_file_size_bytes:,}"
            )
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        existing = self.repository.find_resume_by_sha256(profile.profile_id, digest)
        if existing is not None:
            extractions = self.repository.list_extractions(existing.resume_version_id)
            return IngestionResult(
                resume_version=existing,
                extraction=extractions[0] if extractions else None,
                duplicate=True,
                activated=False,
                message="This exact file is already stored; nothing new was created.",
            )
        document = extract_document_text(content, self.limits)
        version_id = f"rv-{uuid4().hex[:16]}"
        storage_key = f"resumes/{profile.profile_id}/{version_id}"
        version = ResumeVersion(
            resume_version_id=version_id,
            profile_id=profile.profile_id,
            label=(label or source.stem).strip()[:200] or "Resume",
            original_filename=source.name[:255],
            media_type=document.media_type,
            size_bytes=len(content),
            sha256=digest,
            storage_key=storage_key,
        )
        extraction = self._extract(
            version_id, document.text, document.page_count, document.truncated
        )
        folder = self._private_folder(storage_key)
        _write_atomic(folder / f"original.{document.media_type.value}", content)
        _store_text(folder, document.text)
        try:
            self.repository.add_resume_with_extraction(version, extraction)
        except BaseException:
            for path in folder.iterdir():
                path.unlink(missing_ok=True)
            folder.rmdir()
            raise
        activated, message = False, "Stored. Review its extracted facts before using it."
        if activate:
            activated, message = self._activate(version, extraction)
        return IngestionResult(
            version, extraction, duplicate=False, activated=activated, message=message
        )

    def reextract(self, resume_version_id: str) -> ResumeExtraction:
        """Parse a stored resume again (after a parser change) as a new pending extraction."""
        version = self.repository.get_resume_version(resume_version_id)
        content = (
            self._private_folder(version.storage_key) / f"original.{version.media_type.value}"
        ).read_bytes()
        if hashlib.sha256(content).hexdigest() != version.sha256:
            raise ResumeDocumentError("stored resume file no longer matches its recorded hash")
        document = self._document(version, content)
        extraction = self._extract(
            version.resume_version_id, document.text, document.page_count, document.truncated
        )
        folder = self._private_folder(version.storage_key)
        latest = folder / LATEST_TEXT
        if latest.exists():  # keep the text earlier extractions' evidence spans point into
            previous = latest.read_text(encoding="utf-8")
            snapshot = folder / _text_name(sha256_text(previous))
            if not snapshot.exists():
                _write_atomic(snapshot, previous.encode("utf-8"))
        _store_text(folder, document.text)
        self.repository.add_extraction(extraction)
        self.repository.supersede_others(
            version.resume_version_id, extraction.extraction_id, approved=False
        )
        return extraction

    def revise(self, extraction_id: str) -> ResumeExtraction:
        """Copy an extraction's facts into a new pending review for manual correction.

        Use it to fix an approved (or superseded) review: the copy starts unapproved, the
        source stays as it was, and approving the copy supersedes the older approval.
        """
        source = self.repository.get_extraction(extraction_id)
        version = self.repository.get_resume_version(source.resume_version_id)
        if version.profile_id != self.profile().profile_id:
            raise ProfileRecordNotFoundError(f"extraction not found: {extraction_id}")
        if source.status == ExtractionStatus.FAILED:
            raise ResumeDocumentError(
                "a failed extraction cannot be revised; edit its text instead"
            )
        revision = ResumeExtraction.model_validate(
            {
                **source.model_dump(),
                "extraction_id": f"ex-{uuid4().hex[:16]}",
                "review_status": ReviewStatus.PENDING,
                "approved_sections": [],
                "review_comment": "",
                "revision": 1,
                "created_at": _now(),
                "review_updated_at": None,
                "approved_at": None,
                "superseded": False,
                "superseded_at": None,
                "revised_from_extraction_id": source.extraction_id,
            }
        )
        self.repository.add_extraction(revision)
        self.repository.supersede_others(
            version.resume_version_id, revision.extraction_id, approved=False
        )
        return revision

    def create_edited_version(
        self, source_resume_version_id: str, text: str, *, label: str | None = None
    ) -> IngestionResult:
        """Store corrected resume text as a new version derived from another, and parse it.

        The original upload is never changed. Identical text already stored is reported
        as a duplicate instead of creating another version.
        """
        profile = self.profile()
        source = self.repository.get_resume_version(source_resume_version_id)
        if source.profile_id != profile.profile_id:
            raise ProfileRecordNotFoundError(
                f"resume version not found: {source_resume_version_id}"
            )
        normalized = normalize_text(text)
        if not normalized:
            raise ResumeDocumentError("edited resume text is empty")
        if len(normalized) > self.limits.max_extracted_characters:
            raise ResumeDocumentError(
                f"edited text has {len(normalized):,} characters; "
                f"the limit is {self.limits.max_extracted_characters:,}"
            )
        content = normalized.encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        existing = self.repository.find_resume_by_sha256(profile.profile_id, digest)
        if existing is not None:
            extractions = self.repository.list_extractions(existing.resume_version_id)
            return IngestionResult(
                resume_version=existing,
                extraction=extractions[0] if extractions else None,
                duplicate=True,
                activated=False,
                message="This exact text is already stored; nothing new was created.",
            )
        version_id = f"rv-{uuid4().hex[:16]}"
        storage_key = f"resumes/{profile.profile_id}/{version_id}"
        stem = Path(source.original_filename).stem[:200] or "resume"
        version = ResumeVersion(
            resume_version_id=version_id,
            profile_id=profile.profile_id,
            label=(label or f"{source.label} (edited)").strip()[:200] or "Edited resume",
            original_filename=f"{stem}-edited.txt",
            media_type=ResumeMediaType.TXT,
            size_bytes=len(content),
            sha256=digest,
            storage_key=storage_key,
            derived_from_resume_version_id=source.resume_version_id,
        )
        extraction = self._extract(version_id, normalized, None, False)
        folder = self._private_folder(storage_key)
        _write_atomic(folder / "original.txt", content)
        _store_text(folder, normalized)
        try:
            self.repository.add_resume_with_extraction(version, extraction)
        except BaseException:
            for path in folder.iterdir():
                path.unlink(missing_ok=True)
            folder.rmdir()
            raise
        return IngestionResult(
            version,
            extraction,
            duplicate=False,
            activated=False,
            message="Saved as a new version. Review and approve it, then make it active.",
        )

    def _document(self, version: ResumeVersion, content: bytes) -> DocumentText:
        if version.media_type == ResumeMediaType.TXT:
            text = normalize_text(content.decode("utf-8"))
            return DocumentText(ResumeMediaType.TXT, text, None, False)
        return extract_document_text(content, self.limits)

    def activate(self, resume_version_id: str) -> tuple[bool, str]:
        """Make a stored resume version active if its newest extraction passed quality."""
        version = self.repository.get_resume_version(resume_version_id)
        extractions = self.repository.list_extractions(resume_version_id)
        if not extractions:
            return False, "No extraction exists for this resume version."
        return self._activate(version, extractions[0])

    def extracted_text(self, resume_version_id: str, text_sha256: str | None = None) -> str:
        """Return private extracted text (for review screens).

        With ``text_sha256``, return exactly the text that extraction parsed, so its
        evidence offsets line up even after a re-extraction replaced the latest text.
        """
        version = self.repository.get_resume_version(resume_version_id)
        folder = self._private_folder(version.storage_key)
        if text_sha256 is None:
            return (folder / LATEST_TEXT).read_text(encoding="utf-8")
        for path in (folder / _text_name(text_sha256), folder / LATEST_TEXT):
            if path.exists():
                text = path.read_text(encoding="utf-8")
                if sha256_text(text) == text_sha256:
                    return text
        raise FileNotFoundError("the text this extraction was parsed from is no longer stored")

    def activate_approved(self, resume_version_id: str) -> ResumeVersion:
        """Make a version active for scoring; it must have an approved extraction."""
        version = self.repository.get_resume_version(resume_version_id)
        if version.profile_id != self.profile().profile_id:
            raise ProfileRecordNotFoundError(f"resume version not found: {resume_version_id}")
        if self.repository.latest_approved_extraction(resume_version_id) is None:
            raise ProfileRepositoryConflictError(
                "approve this resume version's review before making it active"
            )
        self.repository.set_active_resume(version.profile_id, resume_version_id)
        return version

    def _activate(self, version: ResumeVersion, extraction: ResumeExtraction) -> tuple[bool, str]:
        if extraction.status == ExtractionStatus.FAILED:
            return False, "Not activated: text extraction failed quality checks."
        self.repository.set_active_resume(version.profile_id, version.resume_version_id)
        return True, "Activated. Review and approve its facts before comparisons use it."

    def _extract(
        self, version_id: str, text: str, page_count: int | None, truncated: bool
    ) -> ResumeExtraction:
        checks, score, status = assess_text_quality(text, self.limits, truncated=truncated)
        parsed = self.parser.parse(text)
        return ResumeExtraction(
            extraction_id=f"ex-{uuid4().hex[:16]}",
            resume_version_id=version_id,
            parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
            text_sha256=sha256_text(text),
            page_count=page_count,
            character_count=len(text),
            quality_score=score,
            quality_checks=checks,
            status=status,
            facts=parsed.facts,
            warnings=parsed.warnings,
        )

    def _private_folder(self, storage_key: str) -> Path:
        root = self.private_root.resolve()
        folder = (root / storage_key).resolve()
        if root not in folder.parents:
            raise ValueError("resume storage key escapes the private data folder")
        return folder


def _now() -> datetime:
    return datetime.now(UTC)


def _text_name(text_sha256: str) -> str:
    return f"extracted-{text_sha256[:16]}.txt"


def _store_text(folder: Path, text: str) -> None:
    """Save extracted text under its hash and as the resume version's latest text."""
    data = text.encode("utf-8")
    _write_atomic(folder / _text_name(sha256_text(text)), data)
    _write_atomic(folder / LATEST_TEXT, data)


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


__all__ = ["DATABASE_NAME", "IngestionResult", "ResumeIngestionService", "ResumeMediaType"]
