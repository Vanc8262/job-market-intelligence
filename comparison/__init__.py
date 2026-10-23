"""Phase 6.3: compare current postings with the approved resume, with evidence."""

from comparison.compare import ResumeView, compare_posting, document_frequencies
from comparison.requirements import Requirement, extract_requirements

__all__ = [
    "Requirement",
    "ResumeView",
    "compare_posting",
    "document_frequencies",
    "extract_requirements",
]
