"""Compare every current opportunity with the approved resume and store the results.

Results contain resume evidence, so they are written to the private folder
(``data/private/comparisons/latest.json``), never to the data repository.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from comparison.compare import ResumeView, compare_posting, document_frequencies
from models.job_posting import JobPosting, PostingStatus
from processors import JobPostingNormalizer
from profiles.profile_source import approved_resume_extraction
from profiles.service import ResumeIngestionService
from utils.config import Config
from utils.hashing import stable_json_hash

RESULTS_FILE = Path("comparisons") / "latest.json"
_INACTIVE = {PostingStatus.DUPLICATE, PostingStatus.EXPIRED, PostingStatus.REMOVED}


@dataclass
class ComparisonResult:
    """Outcome of one comparison run (no resume text)."""

    compared: int = 0
    skipped_reason: str | None = None
    bands: Counter[str] = field(default_factory=Counter)
    output_path: Path | None = None

    def summary(self) -> dict[str, object]:
        """Return a JSON-safe summary."""
        return {
            "compared": self.compared,
            "skipped_reason": self.skipped_reason,
            "bands": dict(self.bands),
            "output_path": str(self.output_path) if self.output_path else None,
        }


def results_path(private_root: Path) -> Path:
    """Return where comparison results are stored."""
    return private_root / RESULTS_FILE


def load_results(private_root: Path) -> dict[str, object] | None:
    """Return stored comparison results, or None when there are none yet."""
    path = results_path(private_root)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


class ApplicationComparisonRunner:
    """Run Phase 6.3 over current Silver postings."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        private_root: Path | None = None,
        silver_dir: Path | None = None,
    ) -> None:
        """Wire configuration, the private folder, Silver, and the shared skill matcher."""
        self.config = config or Config()
        self.private_root = Path(private_root or self.config.settings.paths.private_dir)
        self.silver_dir = Path(silver_dir or self.config.settings.paths.silver_dir)
        self.normalizer = JobPostingNormalizer(self.config)

    def run(self, *, job_ids: set[str] | None = None) -> ComparisonResult:
        """Compare current postings (optionally only ``job_ids``) and save the results."""
        result = ComparisonResult()
        extraction = approved_resume_extraction(self.config, private_root=self.private_root)
        if extraction is None:
            result.skipped_reason = "no approved active resume"
            return result
        service = ResumeIngestionService(self.config, private_root=self.private_root)
        text = service.extracted_text(extraction.resume_version_id, extraction.text_sha256)
        resume = ResumeView.from_facts(extraction.facts, text)
        postings = self._current_postings()
        frequencies, corpus_size = document_frequencies(postings)
        settings = self.config.comparison
        jobs: dict[str, object] = {}
        for posting in postings:
            if job_ids is not None and posting.metadata.job_id not in job_ids:
                continue
            comparison = compare_posting(
                posting,
                resume,
                settings,
                skills_in_text=self.normalizer.skills_in_text,
                frequencies=frequencies,
                corpus_size=corpus_size,
            )
            jobs[posting.metadata.job_id] = comparison
            result.bands[str(comparison["band"])] += 1
        result.compared = len(jobs)
        payload: dict[str, object] = {
            "generated_at": datetime.now(UTC).isoformat(),
            "model": {
                "name": "application_comparison",
                "phase": "6.3",
                "version": settings.version,
                "settings_hash": stable_json_hash(settings.model_dump(mode="json"))[:16],
            },
            "resume": {
                "resume_version_id": extraction.resume_version_id,
                "extraction_id": extraction.extraction_id,
                "years": resume.years,
            },
            "jobs": jobs,
        }
        result.output_path = results_path(self.private_root)
        _write_json_atomic(result.output_path, payload)
        return result

    def _current_postings(self) -> list[JobPosting]:
        silver = self.silver_dir
        postings: list[JobPosting] = []
        for path in sorted(silver.rglob("*.json")):
            try:
                posting = JobPosting.model_validate_json(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if posting.metadata.is_active and posting.metadata.status not in _INACTIVE:
                postings.append(posting)
        return postings


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"), default=str)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


__all__ = ["ApplicationComparisonRunner", "ComparisonResult", "load_results", "results_path"]
