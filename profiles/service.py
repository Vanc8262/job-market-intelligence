"""Resume ingestion: store the original privately, extract, score, parse, and record."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from models.candidate import (
    CandidateProfile,
    ExtractionStatus,
    ResumeExtraction,
    ResumeMediaType,
    ResumeVersion,
)
from profiles.documents import ResumeDocumentError, extract_document_text
from profiles.parser import PARSER_NAME, PARSER_VERSION, ResumeParser, SkillVocabulary
from profiles.quality import assess_text_quality
from profiles.repository import CandidateProfileRepository
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
        _write_atomic(folder / "extracted.txt", document.text.encode("utf-8"))
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
        document = extract_document_text(content, self.limits)
        extraction = self._extract(
            version.resume_version_id, document.text, document.page_count, document.truncated
        )
        self.repository.add_extraction(extraction)
        return extraction

    def activate(self, resume_version_id: str) -> tuple[bool, str]:
        """Make a stored resume version active if its newest extraction passed quality."""
        version = self.repository.get_resume_version(resume_version_id)
        extractions = self.repository.list_extractions(resume_version_id)
        if not extractions:
            return False, "No extraction exists for this resume version."
        return self._activate(version, extractions[0])

    def extracted_text(self, resume_version_id: str) -> str:
        """Return the private extracted text of a resume version (for review screens)."""
        version = self.repository.get_resume_version(resume_version_id)
        return (self._private_folder(version.storage_key) / "extracted.txt").read_text(
            encoding="utf-8"
        )

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
