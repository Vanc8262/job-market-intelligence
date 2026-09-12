"""Shared extractor interface and orchestration primitives."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from models.job_posting import JobPosting
from storage.base import BaseStorage
from utils.checkpoint import Checkpoint, CheckpointManager
from utils.config import Config, SourceDefinition
from utils.hashing import stable_json_hash
from utils.logger import logger


@dataclass(frozen=True)
class SearchRequest:
    """Source-agnostic search request accepted by extractor implementations."""

    query: str
    location: str | None = None
    filters: dict[str, object] = field(default_factory=dict)

@dataclass(frozen=True)
class ExtractionResult:
    """Summary produced by a shared extraction run."""

    source_name: str
    request: SearchRequest
    references_found: int
    postings: list[JobPosting]
    raw_records_saved: int = 0
    successful_references: list[str] = field(default_factory=list)
    failed_references: dict[str, str] = field(default_factory=dict)

    @property
    def succeeded(self) -> int:
        """Return the number of references fetched and parsed successfully."""
        return len(self.postings)

    @property
    def failed(self) -> int:
        """Return the number of references that failed during extraction."""
        return len(self.failed_references)

class BaseExtractor(ABC):
    """Contract and shared workflow for every source-specific extractor.

    Concrete extractors provide source-specific search, fetch, and parse behavior.
    This class centralizes raw preservation, configuration, checkpoints, and logging.
    """

    def __init__(
        self,
        source_name: str,
        *,
        config: Config | None = None,
        bronze_storage: BaseStorage | None = None,
        checkpoint_manager: CheckpointManager | None = None,
    ) -> None:
        """Initialize an extractor for one configured source."""
        self.source_name = source_name
        self.config = config or Config()
        self.bronze_storage = bronze_storage
        self.checkpoint_manager = checkpoint_manager

    @property
    def source_definition(self) -> SourceDefinition | None:
        """Return this extractor's configured source definition, if present."""
        return self.config.sources.sources.get(self.source_name)

    @property
    def checkpoint_name(self) -> str:
        """Return the stable checkpoint name for this source."""
        return f"{self.source_name}-extractor"

    def extract(
        self,
        query: str,
        location: str | None = None,
        *,
        limit: int | None = None,
        preserve_raw: bool = True,
        **filters: object,
    ) -> ExtractionResult:
        """Run a complete source-agnostic extraction cycle.

        The workflow searches for source references, fetches each raw payload,
        optionally saves the raw record to bronze storage, parses canonical
        postings, and periodically checkpoints progress.
        """
        request = SearchRequest(query=query, location=location, filters=dict(filters))
        references = self.search_jobs(request.query, request.location, **request.filters)
        if limit is not None:
            references = references[:limit]

        return self.extract_references(
            request,
            references,
            preserve_raw=preserve_raw,
        )

    def extract_references(
        self,
        request: SearchRequest,
        references: list[str],
        *,
        preserve_raw: bool = True,
    ) -> ExtractionResult:
        """Fetch and parse a prepared set of source references.

        Multi-query collection can union and deduplicate search results before
        invoking the same preservation, parsing, failure, and checkpoint logic.
        """
        postings: list[JobPosting] = []
        successful_references: list[str] = []
        failed_references: dict[str, str] = {}
        raw_records_saved = 0
        checkpoint_interval = self.config.settings.scraping.checkpoint_interval

        logger.info(
            "Starting extraction for {source}: {count} references",
            source=self.source_name,
            count=len(references),
        )

        for position, reference in enumerate(references, start=1):
            try:
                raw_job = self.fetch_job(reference)
                if preserve_raw:
                    if self.save_raw(raw_job) is not None:
                        raw_records_saved += 1
                posting = self.parse_job(raw_job)
                postings.append(posting)
                successful_references.append(reference)
            except Exception as exc:
                failed_references[reference] = str(exc)
                logger.error(
                    "Extractor {source} failed for reference {reference}: {error}",
                    source=self.source_name,
                    reference=reference,
                    error=exc,
                )

            if position % checkpoint_interval == 0:
                self.save_checkpoint(
                    position=position,
                    state={"query": request.query, "location": request.location},
                )

        self.save_checkpoint(
            position=len(references),
            state={
                "query": request.query,
                "location": request.location,
                "references_found": len(references),
                "succeeded": len(postings),
                "failed": len(failed_references),
            },
        )

        return ExtractionResult(
            source_name=self.source_name,
            request=request,
            references_found=len(references),
            postings=postings,
            raw_records_saved=raw_records_saved,
            successful_references=successful_references,
            failed_references=failed_references,
        )

    def load_checkpoint(self) -> Checkpoint | None:
        """Load this extractor's last checkpoint when configured."""
        if self.checkpoint_manager is None:
            return None
        return self.checkpoint_manager.load(self.checkpoint_name)

    def save_checkpoint(self, position: str | int | None, state: dict[str, Any]) -> Path | None:
        """Persist extractor progress when checkpointing is configured."""
        if self.checkpoint_manager is None:
            return None
        return self.checkpoint_manager.save(
            Checkpoint(name=self.checkpoint_name, position=position, state=state)
        )

    def raw_storage_key(self, raw_job: dict[str, Any]) -> str:
        """Return a stable Bronze storage key for a raw source payload."""
        external_id = self._raw_external_id(raw_job)
        identity = external_id or stable_json_hash(raw_job)
        return f"{self.source_name}/{identity}"

    def save_raw(self, raw_job: dict[str, Any]) -> Path | None:
        """Persist a raw payload through the shared Bronze storage interface."""
        if self.bronze_storage is None:
            raise RuntimeError("bronze_storage is required to preserve raw extractor payloads")
        return self.bronze_storage.save(self.raw_storage_key(raw_job), raw_job)

    @abstractmethod
    def search_jobs(self, query: str, location: str | None = None, **filters: object) -> list[str]:
        """Search a source and return source-specific job references."""
        raise NotImplementedError

    @abstractmethod
    def fetch_job(self, job_reference: str) -> dict[str, Any]:
        """Fetch a raw job payload from a source-specific reference."""
        raise NotImplementedError

    @abstractmethod
    def parse_job(self, raw_job: dict[str, Any]) -> JobPosting:
        """Parse a raw source payload into the canonical posting model."""
        raise NotImplementedError

    @staticmethod
    def _raw_external_id(raw_job: dict[str, Any]) -> str | None:
        for key in ("id", "job_id", "external_id", "absolute_url", "url"):
            value = raw_job.get(key)
            if value:
                return str(value)
        return None
