"""Configuration-driven extraction orchestration with Bronze preservation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from extractors.base_extractor import ExtractionResult
from extractors.registry import ExtractorRegistry
from utils.logger import log_event


@dataclass(frozen=True)
class TargetExtractionOutcome:
    """Outcome for one configured extraction target."""

    target_name: str
    source_name: str
    result: ExtractionResult | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """Return whether target-level extraction completed."""
        return self.result is not None and self.error is None

    def summary(self) -> dict[str, object]:
        """Return a JSON-serializable operational summary."""
        return {
            "target": self.target_name,
            "source": self.source_name,
            "succeeded": self.succeeded,
            "references_found": self.result.references_found if self.result else 0,
            "postings_extracted": self.result.succeeded if self.result else 0,
            "posting_failures": self.result.failed if self.result else 0,
            "raw_records_saved": self.result.raw_records_saved if self.result else 0,
            "error": self.error,
        }


@dataclass(frozen=True)
class ExtractionBatchResult:
    """Aggregate result for one multi-target extraction run."""

    run_id: str
    query: str
    location: str | None
    started_at: datetime
    finished_at: datetime
    outcomes: list[TargetExtractionOutcome] = field(default_factory=list)

    @property
    def targets_succeeded(self) -> int:
        """Return the number of targets that completed extraction."""
        return sum(outcome.succeeded for outcome in self.outcomes)

    @property
    def targets_failed(self) -> int:
        """Return the number of targets that failed before completion."""
        return len(self.outcomes) - self.targets_succeeded

    @property
    def references_found(self) -> int:
        """Return total source references found across completed targets."""
        return sum(
            outcome.result.references_found
            for outcome in self.outcomes
            if outcome.result is not None
        )

    @property
    def postings_extracted(self) -> int:
        """Return total canonical postings extracted."""
        return sum(
            outcome.result.succeeded
            for outcome in self.outcomes
            if outcome.result is not None
        )

    @property
    def posting_failures(self) -> int:
        """Return total per-reference failures from completed targets."""
        return sum(
            outcome.result.failed for outcome in self.outcomes if outcome.result is not None
        )

    @property
    def raw_records_saved(self) -> int:
        """Return total new immutable Bronze snapshots saved."""
        return sum(
            outcome.result.raw_records_saved
            for outcome in self.outcomes
            if outcome.result is not None
        )

    def summary(self) -> dict[str, object]:
        """Return a JSON-serializable batch summary."""
        return {
            "run_id": self.run_id,
            "query": self.query,
            "location": self.location,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "targets_attempted": len(self.outcomes),
            "targets_succeeded": self.targets_succeeded,
            "targets_failed": self.targets_failed,
            "references_found": self.references_found,
            "postings_extracted": self.postings_extracted,
            "posting_failures": self.posting_failures,
            "raw_records_saved": self.raw_records_saved,
            "targets": [outcome.summary() for outcome in self.outcomes],
        }


class ExtractionRunner:
    """Run configured extractors while guaranteeing Bronze preservation."""

    def __init__(self, registry: ExtractorRegistry | None = None) -> None:
        """Initialize with a configured extractor registry."""
        self.registry = registry or ExtractorRegistry()

    def run(
        self,
        query: str,
        location: str | None = None,
        *,
        target_names: list[str] | None = None,
        limit: int | None = None,
        filters_by_target: dict[str, dict[str, object]] | None = None,
        client: httpx.Client | None = None,
        run_id: str | None = None,
    ) -> ExtractionBatchResult:
        """Extract selected or enabled targets and preserve every raw payload.

        Target-level exceptions are captured as outcomes so one unavailable
        career site does not prevent other configured targets from running.
        """
        active_run_id = run_id or uuid4().hex
        started_at = datetime.now(UTC)
        selected_names = target_names if target_names is not None else self.registry.enabled_names
        target_filters = filters_by_target or {}
        outcomes: list[TargetExtractionOutcome] = []

        log_event(
            "INFO",
            "Starting extraction batch for {count} targets",
            component="extraction_pipeline",
            event="extraction_batch_started",
            run_id=active_run_id,
            count=len(selected_names),
        )

        for target_name in selected_names:
            extractor = None
            try:
                extractor = self.registry.create(target_name, client=client)
                result = extractor.extract(
                    query,
                    location,
                    limit=limit,
                    preserve_raw=True,
                    **target_filters.get(target_name, {}),
                )
                outcomes.append(
                    TargetExtractionOutcome(
                        target_name=target_name,
                        source_name=extractor.source_name,
                        result=result,
                    )
                )
                log_event(
                    "SUCCESS",
                    "Completed target {target}: {count} postings, {raw} raw snapshots",
                    component="extraction_pipeline",
                    event="extraction_target_completed",
                    source=extractor.source_name,
                    run_id=active_run_id,
                    target=target_name,
                    count=result.succeeded,
                    raw=result.raw_records_saved,
                )
            except Exception as exc:
                source_name = extractor.source_name if extractor is not None else "-"
                outcomes.append(
                    TargetExtractionOutcome(
                        target_name=target_name,
                        source_name=source_name,
                        error=str(exc),
                    )
                )
                log_event(
                    "ERROR",
                    "Extraction target {target} failed: {error}",
                    component="extraction_pipeline",
                    event="extraction_target_failed",
                    source=source_name,
                    run_id=active_run_id,
                    target=target_name,
                    error=exc,
                )
            finally:
                if extractor is not None:
                    close = getattr(extractor, "close", None)
                    if callable(close):
                        close()

        batch = ExtractionBatchResult(
            run_id=active_run_id,
            query=query,
            location=location,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            outcomes=outcomes,
        )
        log_event(
            "SUCCESS" if batch.targets_failed == 0 else "WARNING",
            "Extraction batch finished: {succeeded} succeeded, {failed} failed",
            component="extraction_pipeline",
            event="extraction_batch_finished",
            run_id=active_run_id,
            succeeded=batch.targets_succeeded,
            failed=batch.targets_failed,
            raw_records_saved=batch.raw_records_saved,
        )
        return batch


__all__ = ["ExtractionBatchResult", "ExtractionRunner", "TargetExtractionOutcome"]
