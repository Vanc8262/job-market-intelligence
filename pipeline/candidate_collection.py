"""Configurable, deduplicated multi-query candidate collection."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from extractors.base_extractor import ExtractionResult, SearchRequest
from extractors.registry import ExtractorRegistry
from storage import GoldStorage
from utils.config import CandidateQueryConfig, Config
from utils.logger import log_event


@dataclass(frozen=True)
class CandidateQueryOutcome:
    """Search result for one query against one configured target."""

    query_name: str
    query: str
    location: str | None
    references_found: int
    new_references: int
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """Return whether the source search completed."""
        return self.error is None

    def summary(self) -> dict[str, object]:
        """Return a JSON-serializable query outcome."""
        return {
            "query_name": self.query_name,
            "query": self.query,
            "location": self.location,
            "succeeded": self.succeeded,
            "references_found": self.references_found,
            "new_references": self.new_references,
            "error": self.error,
        }


@dataclass(frozen=True)
class CandidateTargetOutcome:
    """Multi-query collection outcome for one extractor target."""

    target_name: str
    source_name: str
    references_discovered: int
    references_selected: int
    discovered_references: list[str] = field(default_factory=list)
    queries: list[CandidateQueryOutcome] = field(default_factory=list)
    result: ExtractionResult | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """Return whether target construction and detail collection completed."""
        return self.result is not None and self.error is None

    def summary(self) -> dict[str, object]:
        """Return a JSON-serializable target outcome."""
        observations = []
        if self.result is not None:
            observations = [
                {
                    "reference": reference,
                    "job_id": posting.metadata.job_id,
                    "external_id": posting.source.external_id,
                }
                for reference, posting in zip(
                    self.result.successful_references,
                    self.result.postings,
                    strict=True,
                )
            ]
        return {
            "target": self.target_name,
            "source": self.source_name,
            "succeeded": self.succeeded,
            "references_discovered": self.references_discovered,
            "references_selected": self.references_selected,
            "discovered_references": self.discovered_references,
            "observations": observations,
            "postings_extracted": self.result.succeeded if self.result else 0,
            "posting_failures": self.result.failed if self.result else 0,
            "raw_records_saved": self.result.raw_records_saved if self.result else 0,
            "queries": [query.summary() for query in self.queries],
            "error": self.error,
        }


@dataclass(frozen=True)
class CandidateCollectionResult:
    """Aggregate result for one configured candidate collection run."""

    run_id: str
    profile_name: str
    started_at: datetime
    finished_at: datetime
    per_target_limit: int
    query_names: list[str]
    outcomes: list[CandidateTargetOutcome]
    output_paths: dict[str, Path] = field(default_factory=dict)

    @property
    def targets_attempted(self) -> int:
        """Return the number of configured targets attempted."""
        return len(self.outcomes)

    @property
    def targets_failed(self) -> int:
        """Return targets that failed construction or detail collection."""
        return sum(not outcome.succeeded for outcome in self.outcomes)

    @property
    def search_attempts(self) -> int:
        """Return query-target searches attempted."""
        return sum(len(outcome.queries) for outcome in self.outcomes)

    @property
    def search_failures(self) -> int:
        """Return query-target searches that failed."""
        return sum(
            not query.succeeded
            for outcome in self.outcomes
            for query in outcome.queries
        )

    @property
    def references_discovered(self) -> int:
        """Return unique references discovered within each target."""
        return sum(outcome.references_discovered for outcome in self.outcomes)

    @property
    def references_selected(self) -> int:
        """Return references retained after per-target caps."""
        return sum(outcome.references_selected for outcome in self.outcomes)

    @property
    def postings_extracted(self) -> int:
        """Return references fetched and parsed successfully."""
        return sum(
            outcome.result.succeeded
            for outcome in self.outcomes
            if outcome.result is not None
        )

    @property
    def posting_failures(self) -> int:
        """Return selected references that failed fetch or parsing."""
        return sum(
            outcome.result.failed
            for outcome in self.outcomes
            if outcome.result is not None
        )

    @property
    def raw_records_saved(self) -> int:
        """Return new immutable Bronze versions written."""
        return sum(
            outcome.result.raw_records_saved
            for outcome in self.outcomes
            if outcome.result is not None
        )

    @property
    def unique_postings(self) -> int:
        """Return distinct canonical job identifiers across all targets."""
        return len(
            {
                posting.metadata.job_id
                for outcome in self.outcomes
                if outcome.result is not None
                for posting in outcome.result.postings
            }
        )

    @property
    def extraction_failure_rate(self) -> float:
        """Return failed target, search, and detail operations as a share."""
        failed = self.targets_failed + self.search_failures + self.posting_failures
        attempted = self.targets_attempted + self.search_attempts + self.references_selected
        return round(failed / attempted, 4) if attempted else 0.0

    def summary(self) -> dict[str, object]:
        """Return a persisted operational collection summary."""
        return {
            "run_id": self.run_id,
            "profile_name": self.profile_name,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "per_target_limit": self.per_target_limit,
            "query_names": self.query_names,
            "targets_attempted": self.targets_attempted,
            "targets_succeeded": self.targets_attempted - self.targets_failed,
            "targets_failed": self.targets_failed,
            "search_attempts": self.search_attempts,
            "search_failures": self.search_failures,
            "references_discovered": self.references_discovered,
            "references_selected": self.references_selected,
            "postings_extracted": self.postings_extracted,
            "unique_postings": self.unique_postings,
            "posting_failures": self.posting_failures,
            "raw_records_saved": self.raw_records_saved,
            "extraction_failure_rate": self.extraction_failure_rate,
            "targets": [outcome.summary() for outcome in self.outcomes],
        }


class CandidateCollectionRunner:
    """Search configured role queries and fetch each target reference once."""

    def __init__(
        self,
        registry: ExtractorRegistry | None = None,
        *,
        gold_storage: GoldStorage | None = None,
    ) -> None:
        """Initialize shared configuration, extractors, and operational storage."""
        self.registry = registry or ExtractorRegistry()
        self.config: Config = self.registry.config
        self.profile = self.config.candidate_collection
        self.gold_storage = gold_storage or GoldStorage(
            self.config.settings.paths.gold_dir
        )

    def run(
        self,
        *,
        query_names: list[str] | None = None,
        target_names: list[str] | None = None,
        per_target_limit: int | None = None,
        client: httpx.Client | None = None,
        run_id: str | None = None,
    ) -> CandidateCollectionResult:
        """Run selected profile queries with per-target union deduplication."""
        active_run_id = run_id or uuid4().hex
        started_at = datetime.now(UTC)
        queries = self._selected_queries(query_names)
        targets = self._selected_targets(target_names)
        limit = per_target_limit or self.profile.per_target_limit
        if limit < 1:
            raise ValueError("per_target_limit must be greater than zero")

        log_event(
            "INFO",
            "Starting candidate collection for {targets} targets and {queries} queries",
            component="candidate_collection",
            event="candidate_collection_started",
            run_id=active_run_id,
            targets=len(targets),
            queries=len(queries),
        )

        outcomes = [
            self._collect_target(target, queries, limit, client, active_run_id)
            for target in targets
        ]
        result = CandidateCollectionResult(
            run_id=active_run_id,
            profile_name=self.profile.profile_name,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            per_target_limit=limit,
            query_names=[query.name for query in queries],
            outcomes=outcomes,
        )
        summary = result.summary()
        run_path = self.gold_storage.save(f"collection/runs/{active_run_id}", summary)
        latest_path = self.gold_storage.save("collection/latest", summary)
        if run_path is None or latest_path is None:
            raise RuntimeError("Gold storage did not return candidate collection paths")
        result = replace(
            result,
            output_paths={"run": run_path, "latest": latest_path},
        )

        log_event(
            "SUCCESS" if result.extraction_failure_rate == 0 else "WARNING",
            "Candidate collection finished: {postings} postings, {failed} failed operations",
            component="candidate_collection",
            event="candidate_collection_finished",
            run_id=active_run_id,
            postings=result.postings_extracted,
            failed=result.targets_failed + result.search_failures + result.posting_failures,
        )
        return result

    def _collect_target(
        self,
        target_name: str,
        queries: list[CandidateQueryConfig],
        limit: int,
        client: httpx.Client | None,
        run_id: str,
    ) -> CandidateTargetOutcome:
        extractor = None
        query_outcomes: list[CandidateQueryOutcome] = []
        references: list[str] = []
        query_reference_sets: list[list[str]] = []
        seen: set[str] = set()
        source_name = "-"
        try:
            extractor = self.registry.create(target_name, client=client)
            source_name = extractor.source_name
            applicable = [
                query
                for query in queries
                if not query.targets
                or target_name.casefold() in {name.casefold() for name in query.targets}
            ]
            for query in applicable:
                try:
                    found = extractor.search_jobs(query.query, query.location)
                    new = [reference for reference in found if reference not in seen]
                    references.extend(new)
                    seen.update(new)
                    query_reference_sets.append(new)
                    query_outcomes.append(
                        CandidateQueryOutcome(
                            query.name,
                            query.query,
                            query.location,
                            len(found),
                            len(new),
                        )
                    )
                except Exception as exc:
                    query_reference_sets.append([])
                    query_outcomes.append(
                        CandidateQueryOutcome(
                            query.name,
                            query.query,
                            query.location,
                            0,
                            0,
                            str(exc),
                        )
                    )
                    log_event(
                        "ERROR",
                        "Candidate query {query} failed for {target}: {error}",
                        component="candidate_collection",
                        event="candidate_query_failed",
                        source=source_name,
                        run_id=run_id,
                        query=query.name,
                        target=target_name,
                        error=exc,
                    )
            if applicable and all(not outcome.succeeded for outcome in query_outcomes):
                raise RuntimeError("All applicable candidate searches failed")
            selected = self._round_robin_references(query_reference_sets, limit)
            request = SearchRequest(
                query=f"profile:{self.profile.profile_name}",
                filters={"query_names": [query.name for query in applicable]},
            )
            extraction = extractor.extract_references(request, selected, preserve_raw=True)
            return CandidateTargetOutcome(
                target_name=target_name,
                source_name=source_name,
                references_discovered=len(references),
                references_selected=len(selected),
                discovered_references=references,
                queries=query_outcomes,
                result=extraction,
            )
        except Exception as exc:
            return CandidateTargetOutcome(
                target_name=target_name,
                source_name=source_name,
                references_discovered=len(references),
                references_selected=0,
                discovered_references=references,
                queries=query_outcomes,
                error=str(exc),
            )
        finally:
            if extractor is not None:
                close = getattr(extractor, "close", None)
                if callable(close):
                    close()

    def _selected_queries(self, requested: list[str] | None) -> list[CandidateQueryConfig]:
        enabled = {query.name.casefold(): query for query in self.profile.queries if query.enabled}
        if requested is None:
            return list(enabled.values())
        selected: list[CandidateQueryConfig] = []
        for name in requested:
            query = enabled.get(name.casefold())
            if query is None:
                raise ValueError(f"Unknown or disabled candidate query: {name}")
            if query not in selected:
                selected.append(query)
        if not selected:
            raise ValueError("At least one candidate query must be selected")
        return selected

    def _selected_targets(self, requested: list[str] | None) -> list[str]:
        configured = {name.casefold(): name for name in self.registry.enabled_names}
        names = requested if requested is not None else self.profile.target_names
        if not names:
            return self.registry.enabled_names
        selected: list[str] = []
        for name in names:
            target = configured.get(name.casefold())
            if target is None:
                raise ValueError(f"Unknown or disabled candidate target: {name}")
            if target not in selected:
                selected.append(target)
        return selected

    @staticmethod
    def _round_robin_references(reference_sets: list[list[str]], limit: int) -> list[str]:
        """Select references fairly across role queries while preserving order."""
        selected: list[str] = []
        position = 0
        while len(selected) < limit:
            added = False
            for references in reference_sets:
                if position < len(references):
                    selected.append(references[position])
                    added = True
                    if len(selected) == limit:
                        break
            if not added:
                break
            position += 1
        return selected


__all__ = [
    "CandidateCollectionResult",
    "CandidateCollectionRunner",
    "CandidateQueryOutcome",
    "CandidateTargetOutcome",
]
