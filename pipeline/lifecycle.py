"""Posting lifecycle tracking from complete candidate collection evidence."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from models.job_posting import JobPosting, PostingStatus
from models.lifecycle import (
    LifecycleEntry,
    LifecycleEvent,
    LifecycleEventType,
    LifecycleState,
    LifecycleStatus,
)
from storage import GoldStorage, SilverStorage
from utils.config import Config, LifecycleConfig
from utils.hashing import stable_json_hash
from utils.logger import log_event


@dataclass(frozen=True)
class LifecycleResult:
    """Result of applying one candidate collection run to lifecycle state."""

    run_id: str
    collection_run_id: str
    observed_at: datetime
    tracked_postings: int
    current_counts: dict[str, int]
    events: list[LifecycleEvent] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    already_applied: bool = False
    output_paths: dict[str, Path] = field(default_factory=dict)

    @property
    def event_counts(self) -> dict[str, int]:
        """Return event totals grouped by transition type."""
        counts = Counter(event.event_type.value for event in self.events)
        return {event_type.value: counts[event_type.value] for event_type in LifecycleEventType}

    def summary(self) -> dict[str, object]:
        """Return a JSON-serializable lifecycle run report."""
        return {
            "run_id": self.run_id,
            "collection_run_id": self.collection_run_id,
            "observed_at": self.observed_at.isoformat(),
            "already_applied": self.already_applied,
            "tracked_postings": self.tracked_postings,
            "current_counts": self.current_counts,
            "event_counts": self.event_counts,
            "events": [event.model_dump(mode="json") for event in self.events],
            "failures": self.failures,
            "outputs": {name: str(path) for name, path in self.output_paths.items()},
        }


@dataclass(frozen=True)
class _TargetEvidence:
    """Complete search evidence for one target and query scope."""

    target_name: str
    source_name: str
    profile_name: str
    query_names: list[str]
    discovered_references: set[str]
    observations: dict[str, str]


class PostingLifecycleRunner:
    """Track discoveries, changes, disappearances, removals, and reactivations."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        silver_storage: SilverStorage | None = None,
        gold_storage: GoldStorage | None = None,
        lifecycle_config: LifecycleConfig | None = None,
    ) -> None:
        """Initialize canonical and lifecycle storage."""
        self.config = config or Config()
        self.lifecycle = lifecycle_config or self.config.lifecycle
        self.silver_storage = silver_storage or SilverStorage(self.config.settings.paths.silver_dir)
        self.gold_storage = gold_storage or GoldStorage(self.config.settings.paths.gold_dir)

    def run(
        self,
        *,
        collection_summary: Mapping[str, object] | None = None,
        run_id: str | None = None,
    ) -> LifecycleResult:
        """Apply one persisted or supplied collection summary idempotently."""
        summary = collection_summary or self._load_latest_collection()
        collection_run_id = self._required_string(summary, "run_id")
        observed_at = self._parse_datetime(summary.get("finished_at"))
        active_run_id = run_id or uuid4().hex
        state = self._load_state()

        if state.last_collection_run_id == collection_run_id:
            return LifecycleResult(
                run_id=active_run_id,
                collection_run_id=collection_run_id,
                observed_at=observed_at,
                tracked_postings=len(state.entries),
                current_counts=self._current_counts(state),
                already_applied=True,
            )

        evidence = self._complete_target_evidence(summary)
        silver, silver_failures = self._load_silver()
        events: list[LifecycleEvent] = []
        failures = list(silver_failures)

        log_event(
            "INFO",
            "Applying collection {collection_run_id} to posting lifecycle",
            component="posting_lifecycle",
            event="lifecycle_started",
            run_id=active_run_id,
            collection_run_id=collection_run_id,
            complete_targets=len(evidence),
        )

        for target in evidence:
            self._apply_target(
                state,
                target,
                silver,
                observed_at,
                collection_run_id,
                events,
                failures,
            )

        state.last_collection_run_id = collection_run_id
        state.updated_at = observed_at
        result = LifecycleResult(
            run_id=active_run_id,
            collection_run_id=collection_run_id,
            observed_at=observed_at,
            tracked_postings=len(state.entries),
            current_counts=self._current_counts(state),
            events=events,
            failures=failures,
        )
        output_paths = self._persist(state, result)
        result = replace(result, output_paths=output_paths)
        log_event(
            "SUCCESS" if not failures else "WARNING",
            "Lifecycle finished with {tracked} tracked postings and {events} transitions",
            component="posting_lifecycle",
            event="lifecycle_finished",
            run_id=active_run_id,
            collection_run_id=collection_run_id,
            tracked=result.tracked_postings,
            events=len(events),
            failures=len(failures),
        )
        return result

    def _apply_target(
        self,
        state: LifecycleState,
        evidence: _TargetEvidence,
        silver: dict[str, JobPosting],
        observed_at: datetime,
        collection_run_id: str,
        events: list[LifecycleEvent],
        failures: list[str],
    ) -> None:
        scope = sorted(evidence.query_names)
        target_entries = {
            key: entry
            for key, entry in state.entries.items()
            if entry.target_name == evidence.target_name
            and entry.profile_name == evidence.profile_name
            and sorted(entry.query_names) == scope
        }

        for reference, job_id in evidence.observations.items():
            posting = silver.get(job_id)
            if posting is None:
                failures.append(
                    f"{evidence.target_name}:{reference}: Silver job {job_id} was not found"
                )
                continue
            if posting.metadata.status == PostingStatus.DUPLICATE:
                continue
            key = self._entry_key(evidence.target_name, reference)
            entry = state.entries.get(key)
            was_inactive = entry is not None and entry.status != LifecycleStatus.ACTIVE
            if entry is None:
                entry = LifecycleEntry(
                    target_name=evidence.target_name,
                    source_name=posting.source.name,
                    source_reference=reference,
                    job_id=job_id,
                    external_id=posting.source.external_id,
                    title=posting.position.normalized_title or posting.position.title,
                    employer=posting.company.normalized_name or posting.company.name,
                    profile_name=evidence.profile_name,
                    query_names=scope,
                    first_seen_at=posting.metadata.first_seen_at,
                    last_seen_at=observed_at,
                    last_changed_at=posting.updated_at,
                    content_hash=posting.metadata.content_hash,
                )
                state.entries[key] = entry
                events.append(
                    self._event(
                        LifecycleEventType.DISCOVERED,
                        entry,
                        observed_at,
                        collection_run_id,
                        previous_status=None,
                    )
                )
            else:
                self._observe_entry(entry, posting, observed_at, collection_run_id, events)
            silver[job_id] = self._save_observation(
                posting,
                observed_at,
                collection_run_id,
                reactivate=was_inactive,
            )

        for entry in target_entries.values():
            if entry.source_reference in evidence.observations:
                continue
            posting = silver.get(entry.job_id)
            if entry.source_reference in evidence.discovered_references:
                was_inactive = entry.status != LifecycleStatus.ACTIVE
                if was_inactive:
                    events.append(
                        self._event(
                            LifecycleEventType.REACTIVATED,
                            entry,
                            observed_at,
                            collection_run_id,
                            previous_status=entry.status,
                            current_status=LifecycleStatus.ACTIVE,
                        )
                    )
                entry.status = LifecycleStatus.ACTIVE
                entry.last_seen_at = observed_at
                entry.removed_at = None
                entry.consecutive_missing_runs = 0
                entry.observation_count += 1
                if posting is not None:
                    silver[entry.job_id] = self._save_observation(
                        posting,
                        observed_at,
                        collection_run_id,
                        reactivate=was_inactive,
                    )
                continue

            previous_status = entry.status
            entry.consecutive_missing_runs += 1
            if entry.consecutive_missing_runs >= self.lifecycle.missing_runs_before_removed:
                entry.status = LifecycleStatus.REMOVED
                entry.removed_at = observed_at
                event_type = LifecycleEventType.REMOVED
                if posting is not None and self.lifecycle.update_silver_status:
                    silver[entry.job_id] = self._save_removed(
                        posting, observed_at, collection_run_id
                    )
            else:
                entry.status = LifecycleStatus.MISSING
                event_type = LifecycleEventType.MISSING
            if entry.status != previous_status:
                events.append(
                    self._event(
                        event_type,
                        entry,
                        observed_at,
                        collection_run_id,
                        previous_status=previous_status,
                    )
                )

    def _observe_entry(
        self,
        entry: LifecycleEntry,
        posting: JobPosting,
        observed_at: datetime,
        collection_run_id: str,
        events: list[LifecycleEvent],
    ) -> None:
        previous_status = entry.status
        previous_hash = entry.content_hash
        if previous_status != LifecycleStatus.ACTIVE:
            events.append(
                self._event(
                    LifecycleEventType.REACTIVATED,
                    entry,
                    observed_at,
                    collection_run_id,
                    previous_status=previous_status,
                    current_status=LifecycleStatus.ACTIVE,
                )
            )
        if previous_hash != posting.metadata.content_hash:
            events.append(
                self._event(
                    LifecycleEventType.CHANGED,
                    entry,
                    observed_at,
                    collection_run_id,
                    previous_status=previous_status,
                    current_status=LifecycleStatus.ACTIVE,
                    previous_content_hash=previous_hash,
                    current_content_hash=posting.metadata.content_hash,
                )
            )
            entry.last_changed_at = observed_at
        entry.status = LifecycleStatus.ACTIVE
        entry.last_seen_at = observed_at
        entry.removed_at = None
        entry.consecutive_missing_runs = 0
        entry.observation_count += 1
        entry.content_hash = posting.metadata.content_hash
        entry.external_id = posting.source.external_id
        entry.title = posting.position.normalized_title or posting.position.title
        entry.employer = posting.company.normalized_name or posting.company.name

    def _save_observation(
        self,
        posting: JobPosting,
        observed_at: datetime,
        collection_run_id: str,
        *,
        reactivate: bool,
    ) -> JobPosting:
        status = posting.metadata.status
        if reactivate and status != PostingStatus.DUPLICATE:
            status = PostingStatus.ACTIVE
        metadata = posting.metadata.model_copy(
            update={
                "last_seen_at": max(posting.metadata.last_seen_at, observed_at),
                "ingestion_batch_id": collection_run_id,
                "status": status,
            }
        )
        updated = posting.model_copy(
            update={
                "metadata": metadata,
                "updated_at": observed_at if reactivate else posting.updated_at,
            },
            deep=True,
        )
        self.silver_storage.save(self._silver_key(updated), updated)
        return updated

    def _save_removed(
        self,
        posting: JobPosting,
        observed_at: datetime,
        collection_run_id: str,
    ) -> JobPosting:
        if posting.metadata.status == PostingStatus.DUPLICATE:
            return posting
        metadata = posting.metadata.model_copy(
            update={
                "ingestion_batch_id": collection_run_id,
                "status": PostingStatus.REMOVED,
                "is_active": False,
            }
        )
        updated = posting.model_copy(
            update={"metadata": metadata, "updated_at": observed_at},
            deep=True,
        )
        self.silver_storage.save(self._silver_key(updated), updated)
        return updated

    def _complete_target_evidence(self, summary: Mapping[str, object]) -> list[_TargetEvidence]:
        profile_name = self._required_string(summary, "profile_name")
        targets = summary.get("targets")
        if not isinstance(targets, list):
            raise ValueError("Collection summary must contain target outcomes")
        evidence: list[_TargetEvidence] = []
        for item in targets:
            if not isinstance(item, dict) or item.get("succeeded") is not True:
                continue
            queries = item.get("queries")
            discovered = item.get("discovered_references")
            observations = item.get("observations")
            if (
                not isinstance(queries, list)
                or not queries
                or not all(
                    isinstance(query, dict) and query.get("succeeded") is True for query in queries
                )
                or not isinstance(discovered, list)
                or not isinstance(observations, list)
            ):
                continue
            query_names = [
                str(query["query_name"])
                for query in queries
                if isinstance(query, dict) and query.get("query_name")
            ]
            observation_map = {
                str(observation["reference"]): str(observation["job_id"])
                for observation in observations
                if isinstance(observation, dict)
                and observation.get("reference")
                and observation.get("job_id")
            }
            evidence.append(
                _TargetEvidence(
                    target_name=str(item.get("target", "")),
                    source_name=str(item.get("source", "")),
                    profile_name=profile_name,
                    query_names=query_names,
                    discovered_references={str(reference) for reference in discovered},
                    observations=observation_map,
                )
            )
        return evidence

    def _load_silver(self) -> tuple[dict[str, JobPosting], list[str]]:
        records: dict[str, JobPosting] = {}
        failures: list[str] = []
        for path in self.silver_storage.root.rglob("*.json"):
            try:
                posting = JobPosting.model_validate(json.loads(path.read_text(encoding="utf-8")))
                records[posting.metadata.job_id] = posting
            except Exception as exc:
                failures.append(f"{path.relative_to(self.silver_storage.root)}: {exc}")
        return records, failures

    def _load_state(self) -> LifecycleState:
        if not self.gold_storage.exists("lifecycle/state"):
            return LifecycleState()
        return LifecycleState.model_validate(self.gold_storage.load("lifecycle/state"))

    def _load_latest_collection(self) -> Mapping[str, object]:
        payload = self.gold_storage.load("collection/latest")
        if not isinstance(payload, dict):
            raise TypeError("Latest collection summary must be a JSON object")
        return cast(dict[str, object], payload)

    def _persist(self, state: LifecycleState, result: LifecycleResult) -> dict[str, Path]:
        paths = {
            "state": self.gold_storage.path_for("lifecycle/state", ".json"),
            "run": self.gold_storage.path_for(
                f"lifecycle/runs/{result.collection_run_id}", ".json"
            ),
            "latest": self.gold_storage.path_for("lifecycle/latest", ".json"),
            "summary": self.gold_storage.path_for("lifecycle/summary", ".json"),
        }
        report = replace(result, output_paths=paths).summary()
        state_path = self.gold_storage.save("lifecycle/state", state.model_dump(mode="json"))
        run_path = self.gold_storage.save(f"lifecycle/runs/{result.collection_run_id}", report)
        latest_path = self.gold_storage.save("lifecycle/latest", report)
        employer_events = Counter(event.employer for event in result.events)
        summary_path = self.gold_storage.save(
            "lifecycle/summary",
            {
                "generated_at": result.observed_at.isoformat(),
                "collection_run_id": result.collection_run_id,
                "tracked_postings": result.tracked_postings,
                "current_counts": result.current_counts,
                "latest_event_counts": result.event_counts,
                "employer_activity": [
                    {"employer": employer, "events": count}
                    for employer, count in employer_events.most_common()
                ],
            },
        )
        saved_paths = [state_path, run_path, latest_path, summary_path]
        if any(path is None for path in saved_paths):
            raise RuntimeError("Gold storage did not return lifecycle output paths")
        return paths

    @staticmethod
    def _event(
        event_type: LifecycleEventType,
        entry: LifecycleEntry,
        detected_at: datetime,
        collection_run_id: str,
        *,
        previous_status: LifecycleStatus | None,
        current_status: LifecycleStatus | None = None,
        previous_content_hash: str | None = None,
        current_content_hash: str | None = None,
    ) -> LifecycleEvent:
        return LifecycleEvent(
            event_type=event_type,
            detected_at=detected_at,
            collection_run_id=collection_run_id,
            target_name=entry.target_name,
            source_name=entry.source_name,
            source_reference=entry.source_reference,
            job_id=entry.job_id,
            title=entry.title,
            employer=entry.employer,
            previous_status=previous_status,
            current_status=current_status or entry.status,
            previous_content_hash=previous_content_hash,
            current_content_hash=current_content_hash or entry.content_hash,
        )

    @staticmethod
    def _entry_key(target_name: str, reference: str) -> str:
        return stable_json_hash({"target_name": target_name, "reference": reference})

    @staticmethod
    def _silver_key(posting: JobPosting) -> str:
        return f"{posting.source.name}/{posting.metadata.job_id}"

    @staticmethod
    def _current_counts(state: LifecycleState) -> dict[str, int]:
        counts = Counter(entry.status.value for entry in state.entries.values())
        return {status.value: counts[status.value] for status in LifecycleStatus}

    @staticmethod
    def _required_string(summary: Mapping[str, object], key: str) -> str:
        value = summary.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"Collection summary requires {key}")
        return value

    @staticmethod
    def _parse_datetime(value: object) -> datetime:
        if not isinstance(value, str):
            raise ValueError("Collection summary requires finished_at")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


__all__ = ["LifecycleResult", "PostingLifecycleRunner"]
