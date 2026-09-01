"""Idempotent Bronze-to-Silver processing orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from extractors.base_extractor import BaseExtractor
from extractors.registry import ExtractorRegistry
from models.job_posting import JobPosting, PostingStatus
from processors import JobPostingNormalizer, JobPostingValidator
from storage import SilverStorage
from utils.config import (
    AshbyTarget,
    GreenhouseTarget,
    LeverTarget,
    NextEraTarget,
    PhenomTarget,
    WorkdayTarget,
)
from utils.logger import log_event


@dataclass(frozen=True)
class ProcessingFailure:
    """One Bronze snapshot or existing Silver record that could not be processed."""

    record: str
    error: str
    target_name: str = "unmapped"

    def summary(self) -> dict[str, str]:
        """Return a JSON-serializable failure summary."""
        return {"record": self.record, "target": self.target_name, "error": self.error}


@dataclass
class TargetProcessingStats:
    """Mutable processing counters for one configured extraction target."""

    records_found: int = 0
    records_processed: int = 0
    silver_created: int = 0
    silver_updated: int = 0
    unchanged: int = 0
    duplicates_marked: int = 0
    failed: int = 0

    def summary(self) -> dict[str, int]:
        """Return a JSON-serializable target summary."""
        return {
            "records_found": self.records_found,
            "records_processed": self.records_processed,
            "silver_created": self.silver_created,
            "silver_updated": self.silver_updated,
            "unchanged": self.unchanged,
            "duplicates_marked": self.duplicates_marked,
            "failed": self.failed,
        }


@dataclass(frozen=True)
class BronzeToSilverResult:
    """Aggregate result from one Bronze-to-Silver processing run."""

    run_id: str
    started_at: datetime
    finished_at: datetime
    targets: dict[str, TargetProcessingStats]
    failures: list[ProcessingFailure] = field(default_factory=list)

    @property
    def records_found(self) -> int:
        """Return the number of routed Bronze snapshots discovered."""
        return sum(stats.records_found for stats in self.targets.values())

    @property
    def records_processed(self) -> int:
        """Return the number of snapshots parsed, normalized, and validated."""
        return sum(stats.records_processed for stats in self.targets.values())

    @property
    def silver_created(self) -> int:
        """Return the number of new Silver records created."""
        return sum(stats.silver_created for stats in self.targets.values())

    @property
    def silver_updated(self) -> int:
        """Return the number of existing Silver records changed."""
        return sum(stats.silver_updated for stats in self.targets.values())

    @property
    def unchanged(self) -> int:
        """Return the number of semantically unchanged Silver records observed."""
        return sum(stats.unchanged for stats in self.targets.values())

    @property
    def duplicates_marked(self) -> int:
        """Return the number of cross-source duplicate observations marked."""
        return sum(stats.duplicates_marked for stats in self.targets.values())

    @property
    def failed(self) -> int:
        """Return the number of records that failed processing."""
        return len(self.failures)

    def summary(self) -> dict[str, object]:
        """Return a JSON-serializable processing summary."""
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "records_found": self.records_found,
            "records_processed": self.records_processed,
            "silver_created": self.silver_created,
            "silver_updated": self.silver_updated,
            "unchanged": self.unchanged,
            "duplicates_marked": self.duplicates_marked,
            "failed": self.failed,
            "targets": {name: stats.summary() for name, stats in self.targets.items()},
            "failures": [failure.summary() for failure in self.failures],
        }


@dataclass(frozen=True)
class _BronzeSnapshot:
    path: Path
    relative_path: str
    target_name: str
    source_name: str
    observed_at: datetime
    priority: int


class BronzeToSilverRunner:
    """Parse immutable Bronze snapshots into normalized canonical Silver records."""

    def __init__(
        self,
        registry: ExtractorRegistry | None = None,
        *,
        silver_storage: SilverStorage | None = None,
        normalizer: JobPostingNormalizer | None = None,
        validator: JobPostingValidator | None = None,
    ) -> None:
        """Initialize processing dependencies from project configuration."""
        self.registry = registry or ExtractorRegistry()
        self.silver_storage = silver_storage or SilverStorage(
            self.registry.config.settings.paths.silver_dir
        )
        self.normalizer = normalizer or JobPostingNormalizer(self.registry.config)
        self.validator = validator or JobPostingValidator()

    def run(
        self,
        *,
        target_names: list[str] | None = None,
        limit: int | None = None,
        run_id: str | None = None,
    ) -> BronzeToSilverResult:
        """Process configured Bronze routes while isolating record-level failures."""
        if limit is not None and limit < 1:
            raise ValueError("limit must be greater than zero")

        active_run_id = run_id or uuid4().hex
        started_at = datetime.now(UTC)
        selected_names = self._selected_target_names(target_names)
        target_stats = {name: TargetProcessingStats() for name in selected_names}
        snapshots, failures = self._discover_snapshots(selected_names)
        if limit is not None:
            snapshots = snapshots[:limit]

        for snapshot in snapshots:
            target_stats[snapshot.target_name].records_found += 1
        for failure in failures:
            stats = target_stats.get(failure.target_name)
            if stats is not None:
                stats.failed += 1

        existing, silver_failures = self._load_existing_silver()
        failures.extend(silver_failures)
        duplicate_owners = self._duplicate_owners(existing)
        extractors: dict[str, BaseExtractor] = {}

        log_event(
            "INFO",
            "Starting Bronze-to-Silver processing for {count} snapshots",
            component="processing_pipeline",
            event="bronze_to_silver_started",
            run_id=active_run_id,
            count=len(snapshots),
        )

        try:
            for snapshot in snapshots:
                stats = target_stats[snapshot.target_name]
                try:
                    extractor = extractors.get(snapshot.target_name)
                    if extractor is None:
                        extractor = self.registry.create(
                            snapshot.target_name,
                            allow_disabled=True,
                        )
                        extractors[snapshot.target_name] = extractor
                    raw_payload = self._load_bronze_mapping(snapshot.path)
                    parsed = extractor.parse_job(raw_payload)
                    posting = self.normalizer.normalize(parsed).with_derived_metadata()
                    posting = self._apply_observation(posting, snapshot.observed_at, active_run_id)

                    job_id = posting.metadata.job_id
                    previous = existing.get(job_id)
                    self._release_changed_duplicate_owner(previous, posting, duplicate_owners)
                    duplicate_owner = duplicate_owners.get(posting.metadata.duplicate_hash or "")
                    if duplicate_owner is not None and duplicate_owner != job_id:
                        posting = posting.model_copy(
                            update={
                                "metadata": posting.metadata.model_copy(
                                    update={
                                        "status": PostingStatus.DUPLICATE,
                                        "is_active": False,
                                    }
                                )
                            },
                            deep=True,
                        )
                        stats.duplicates_marked += 1
                    else:
                        duplicate_hash = posting.metadata.duplicate_hash
                        if duplicate_hash:
                            duplicate_owners[duplicate_hash] = job_id

                    posting = self._merge_existing(posting, previous)
                    validated = self.validator.validate(posting.model_dump())
                    self.silver_storage.save(self._silver_key(validated), validated)
                    stats.records_processed += 1

                    if previous is None:
                        stats.silver_created += 1
                    elif self._semantic_signature(previous) != self._semantic_signature(validated):
                        stats.silver_updated += 1
                    else:
                        stats.unchanged += 1
                    existing[job_id] = validated
                except Exception as exc:
                    stats.failed += 1
                    failure = ProcessingFailure(
                        record=snapshot.relative_path,
                        target_name=snapshot.target_name,
                        error=str(exc),
                    )
                    failures.append(failure)
                    log_event(
                        "ERROR",
                        "Bronze record failed processing: {record}: {error}",
                        component="processing_pipeline",
                        event="bronze_record_failed",
                        source=snapshot.source_name,
                        run_id=active_run_id,
                        target=snapshot.target_name,
                        record=snapshot.relative_path,
                        error=exc,
                    )
        finally:
            for extractor in extractors.values():
                close = getattr(extractor, "close", None)
                if callable(close):
                    close()

        result = BronzeToSilverResult(
            run_id=active_run_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            targets=target_stats,
            failures=failures,
        )
        log_event(
            "SUCCESS" if result.failed == 0 else "WARNING",
            "Bronze-to-Silver finished: {processed} processed, {failed} failed",
            component="processing_pipeline",
            event="bronze_to_silver_finished",
            run_id=active_run_id,
            processed=result.records_processed,
            failed=result.failed,
            created=result.silver_created,
            updated=result.silver_updated,
            unchanged=result.unchanged,
            duplicates=result.duplicates_marked,
        )
        return result

    def _selected_target_names(self, requested: list[str] | None) -> list[str]:
        configured = {name.casefold(): name for name in self.registry.configured_names}
        if requested is None:
            return self.registry.configured_names
        selected: list[str] = []
        for name in requested:
            configured_name = configured.get(name.casefold())
            if configured_name is None:
                raise ValueError(f"Unknown extractor target: {name}")
            if configured_name not in selected:
                selected.append(configured_name)
        return selected

    def _discover_snapshots(
        self, selected_names: list[str]
    ) -> tuple[list[_BronzeSnapshot], list[ProcessingFailure]]:
        routes = self._configured_routes()
        priorities = {name: index for index, name in enumerate(selected_names)}
        selected = set(selected_names)
        snapshots: list[_BronzeSnapshot] = []
        failures: list[ProcessingFailure] = []

        for path in self.registry.bronze_storage.root.rglob("*.json"):
            relative = path.relative_to(self.registry.bronze_storage.root)
            try:
                route, source_name = self._route_from_path(relative)
                target_name = routes.get(route)
                if target_name is None:
                    if selected == set(self.registry.configured_names):
                        failures.append(
                            ProcessingFailure(
                                record=relative.as_posix(),
                                error=(
                                    "No configured extractor target matches "
                                    f"Bronze route {route}"
                                ),
                            )
                        )
                    continue
                if target_name not in selected:
                    continue
                snapshots.append(
                    _BronzeSnapshot(
                        path=path,
                        relative_path=relative.as_posix(),
                        target_name=target_name,
                        source_name=source_name,
                        observed_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
                        priority=priorities[target_name],
                    )
                )
            except Exception as exc:
                failures.append(
                    ProcessingFailure(record=relative.as_posix(), error=str(exc))
                )

        snapshots.sort(
            key=lambda item: (
                item.priority,
                item.observed_at,
                item.relative_path.casefold(),
            )
        )
        return snapshots, failures

    def _configured_routes(self) -> dict[tuple[str, ...], str]:
        routes: dict[tuple[str, ...], str] = {}
        for target in self.registry.targets.targets:
            route: tuple[str, ...]
            if isinstance(target, GreenhouseTarget):
                route = (target.source, target.board_token.casefold())
            elif isinstance(target, LeverTarget):
                route = (target.source, target.site.casefold())
            elif isinstance(target, AshbyTarget):
                route = (target.source, target.job_board_name.casefold())
            elif isinstance(target, WorkdayTarget):
                route = (target.source, target.tenant.casefold(), target.site.casefold())
            elif isinstance(target, NextEraTarget):
                route = (target.source, target.site.casefold())
            elif isinstance(target, PhenomTarget):
                route = (target.source, target.site.casefold())
            else:
                continue
            if route in routes:
                raise ValueError(f"Multiple extractor targets share Bronze route {route}")
            routes[route] = target.name
        return routes

    @staticmethod
    def _route_from_path(relative: Path) -> tuple[tuple[str, ...], str]:
        parts = relative.parts
        if len(parts) < 4:
            raise ValueError("Bronze path does not contain source, site, job, and version")
        source = parts[0].casefold()
        if source == "workday":
            if len(parts) != 5:
                raise ValueError("Workday Bronze path must contain tenant and career site")
            return (source, parts[1].casefold(), parts[2].casefold()), source
        if len(parts) != 4:
            raise ValueError("ATS Bronze path has an unexpected number of segments")
        return (source, parts[1].casefold()), source

    def _load_existing_silver(
        self,
    ) -> tuple[dict[str, JobPosting], list[ProcessingFailure]]:
        records: dict[str, JobPosting] = {}
        failures: list[ProcessingFailure] = []
        for path in self.silver_storage.root.rglob("*.json"):
            relative = path.relative_to(self.silver_storage.root).as_posix()
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                posting = self.validator.validate(payload)
                records[posting.metadata.job_id] = posting
            except Exception as exc:
                failures.append(
                    ProcessingFailure(
                        record=f"silver/{relative}",
                        target_name="silver",
                        error=str(exc),
                    )
                )
        return records, failures

    @staticmethod
    def _duplicate_owners(records: dict[str, JobPosting]) -> dict[str, str]:
        owners: dict[str, str] = {}
        for job_id, posting in sorted(records.items()):
            duplicate_hash = posting.metadata.duplicate_hash
            if duplicate_hash and posting.metadata.status != PostingStatus.DUPLICATE:
                owners.setdefault(duplicate_hash, job_id)
        return owners

    @staticmethod
    def _release_changed_duplicate_owner(
        previous: JobPosting | None,
        posting: JobPosting,
        owners: dict[str, str],
    ) -> None:
        if previous is None:
            return
        old_hash = previous.metadata.duplicate_hash
        new_hash = posting.metadata.duplicate_hash
        if old_hash and old_hash != new_hash and owners.get(old_hash) == previous.metadata.job_id:
            del owners[old_hash]

    @staticmethod
    def _load_bronze_mapping(path: Path) -> dict[str, object]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("Bronze job payload must be a JSON object")
        return payload

    @staticmethod
    def _apply_observation(
        posting: JobPosting, observed_at: datetime, run_id: str
    ) -> JobPosting:
        metadata = posting.metadata.model_copy(
            update={
                "ingestion_batch_id": run_id,
                "first_seen_at": observed_at,
                "last_seen_at": observed_at,
            }
        )
        return posting.model_copy(
            update={
                "metadata": metadata,
                "created_at": observed_at,
                "updated_at": observed_at,
            },
            deep=True,
        )

    @staticmethod
    def _merge_existing(posting: JobPosting, previous: JobPosting | None) -> JobPosting:
        if previous is None:
            return posting
        metadata = posting.metadata.model_copy(
            update={
                "first_seen_at": min(
                    previous.metadata.first_seen_at,
                    posting.metadata.first_seen_at,
                ),
                "last_seen_at": max(
                    previous.metadata.last_seen_at,
                    posting.metadata.last_seen_at,
                ),
            }
        )
        changed = BronzeToSilverRunner._semantic_signature(previous) != (
            posting.metadata.content_hash,
            posting.metadata.duplicate_hash,
            posting.metadata.status,
        )
        return posting.model_copy(
            update={
                "metadata": metadata,
                "created_at": min(previous.created_at, posting.created_at),
                "updated_at": posting.updated_at if changed else previous.updated_at,
            },
            deep=True,
        )

    @staticmethod
    def _semantic_signature(posting: JobPosting) -> tuple[str | None, str | None, PostingStatus]:
        return (
            posting.metadata.content_hash,
            posting.metadata.duplicate_hash,
            posting.metadata.status,
        )

    @staticmethod
    def _silver_key(posting: JobPosting) -> str:
        return f"{posting.source.name}/{posting.metadata.job_id}"


__all__ = [
    "BronzeToSilverResult",
    "BronzeToSilverRunner",
    "ProcessingFailure",
    "TargetProcessingStats",
]
