"""Candidate profile and resume workflow (Phase 6)."""

from profiles.documents import ResumeDocumentError, extract_document_text
from profiles.parser import PARSER_NAME, PARSER_VERSION, ResumeParser, SkillVocabulary
from profiles.repository import (
    CandidateProfileRepository,
    ProfileRecordNotFoundError,
    ProfileRepositoryConflictError,
    ProfileRepositoryError,
)
from profiles.service import DATABASE_NAME, IngestionResult, ResumeIngestionService

__all__ = [
    "DATABASE_NAME",
    "PARSER_NAME",
    "PARSER_VERSION",
    "CandidateProfileRepository",
    "IngestionResult",
    "ProfileRecordNotFoundError",
    "ProfileRepositoryConflictError",
    "ProfileRepositoryError",
    "ResumeDocumentError",
    "ResumeIngestionService",
    "ResumeParser",
    "SkillVocabulary",
    "extract_document_text",
]
