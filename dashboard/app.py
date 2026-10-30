"""Local server for the Job Market Intelligence dashboard."""

from __future__ import annotations

import argparse
import json
import socket as socket_module
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socket import socket
from socketserver import BaseServer
from threading import Lock
from typing import Any, ClassVar, cast
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ValidationError

from comparison.label_changes import build_changes, create_snapshot
from comparison.label_review import review_labels, summarize_labels
from comparison.labels import (
    CLIENT_SOURCES,
    VERDICT_ERROR,
    LabelSource,
    RequirementLabelStore,
    labels_path,
)
from comparison.review_queue import build_queue, is_queue_head, public_queue
from comparison.runner import load_results
from dashboard.resumes import load_candidate_profile, load_resume_review, load_resume_reviews
from dashboard.tracking import ApplicationTrackingRepository
from models.candidate import ReviewApproval, ReviewDraft
from profiles import (
    ProfileRecordNotFoundError,
    ProfileRepositoryConflictError,
    ResumeDocumentError,
    ResumeIngestionService,
)
from utils.config import Config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_MART_PATH = PROJECT_ROOT / "data" / "gold" / "current_opportunities" / "mart.json"
DEFAULT_TRACKING_PATH = PROJECT_ROOT / "data" / "application_tracking" / "state.json"
DEFAULT_GOLD_ROOT = PROJECT_ROOT / "data" / "gold"
MARKET_SUMMARY_PATHS = {
    "skills": Path("skills/summary.json"),
    "titles": Path("titles/summary.json"),
    "employers": Path("employers/summary.json"),
    "locations": Path("locations/summary.json"),
    "salaries": Path("salaries/summary.json"),
    "relevance": Path("resume_relevance/summary.json"),
    "lifecycle": Path("lifecycle/summary.json"),
    "data_quality": Path("data_quality/latest.json"),
}
OPERATIONS_PATHS = {
    "collection": Path("collection/latest.json"),
    "quality": Path("data_quality/latest.json"),
    "lifecycle": Path("lifecycle/latest.json"),
    "lifecycle_summary": Path("lifecycle/summary.json"),
    "manifest": Path("manifest/latest.json"),
}
MAX_REQUEST_BYTES = 64 * 1024
MAX_REVIEW_REQUEST_BYTES = 512 * 1024
RESUME_REVIEW_PREFIX = "/api/resume-reviews/"
COMPARISON_PREFIX = "/api/comparisons/"
RESUME_VERSION_PREFIX = "/api/resume-versions/"
ACTIVE_RESUME_PATH = "/api/candidate-profile/active-resume"
REQUIREMENT_LABELS_PATH = "/api/requirement-labels"
LABELS_PATH = "/api/labels"
LABEL_QUEUE_PATH = "/api/labels/queue"
LABEL_CHANGES_PATH = "/api/labels/changes"
LABEL_SNAPSHOTS_PATH = "/api/labels/snapshots"
OPERATIONS_FRESHNESS_HOURS = 36
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class DashboardHTTPServer(ThreadingHTTPServer):
    """Bind one dashboard process exclusively to its configured address."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        """Prevent concurrent Windows processes from sharing the dashboard port."""
        if hasattr(socket_module, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(
                socket_module.SOL_SOCKET,
                socket_module.SO_EXCLUSIVEADDRUSE,
                1,
            )
        super().server_bind()


def _hostname(value: str | None) -> str | None:
    """Return the lowercase hostname from a Host header or Origin URL."""
    if not value:
        return None
    candidate = value.strip()
    if "://" not in candidate:
        candidate = f"//{candidate}"
    try:
        hostname = urlsplit(candidate).hostname
    except ValueError:
        return None
    return hostname.casefold() if hostname else None


def load_opportunities_mart(path: Path = DEFAULT_MART_PATH) -> dict[str, Any]:
    """Load and minimally validate the current-opportunities Gold mart."""
    if not path.exists():
        raise FileNotFoundError(
            f"Current-opportunities mart not found: {path}. Run scripts/build_gold.py first."
        )

    payload = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(payload, dict) or not isinstance(payload.get("opportunities"), list):
        raise ValueError("Current-opportunities mart must contain an opportunities list")

    return cast(dict[str, Any], payload)


def load_market_intelligence(gold_root: Path = DEFAULT_GOLD_ROOT) -> dict[str, Any]:
    """Load the reporting summaries as one stable dashboard payload."""
    summaries: dict[str, Any] = {}
    for name, relative_path in MARKET_SUMMARY_PATHS.items():
        path = gold_root / relative_path
        if not path.exists():
            raise FileNotFoundError(
                f"Market summary not found: {path}. Run scripts/build_gold.py first."
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Market summary must be a JSON object: {path}")
        summaries[name] = payload

    generated_values = [
        summary.get("generated_at")
        for summary in summaries.values()
        if isinstance(summary.get("generated_at"), str)
    ]
    return {
        "generated_at": max(generated_values, default=None),
        "summaries": summaries,
    }


def _load_json_object(path: Path) -> dict[str, Any]:
    """Load one required JSON object used by the reporting API."""
    if not path.exists():
        raise FileNotFoundError(f"Pipeline output not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Pipeline output must be a JSON object: {path}")
    return cast(dict[str, Any], payload)


def _stage_freshness(timestamp: object, now: datetime) -> dict[str, object]:
    """Classify one persisted stage timestamp against the operations threshold."""
    if not isinstance(timestamp, str):
        return {
            "status": "missing",
            "age_hours": None,
            "threshold_hours": OPERATIONS_FRESHNESS_HOURS,
        }
    try:
        observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
    except ValueError:
        return {
            "status": "invalid",
            "age_hours": None,
            "threshold_hours": OPERATIONS_FRESHNESS_HOURS,
        }
    age_hours = max(0.0, (now - observed.astimezone(UTC)).total_seconds() / 3600)
    return {
        "status": "stale" if age_hours > OPERATIONS_FRESHNESS_HOURS else "fresh",
        "age_hours": round(age_hours, 1),
        "threshold_hours": OPERATIONS_FRESHNESS_HOURS,
    }


def load_pipeline_operations(
    gold_root: Path = DEFAULT_GOLD_ROOT,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a compact monitoring payload from the latest pipeline outputs."""
    source = {
        name: _load_json_object(gold_root / relative_path)
        for name, relative_path in OPERATIONS_PATHS.items()
    }
    collection = source["collection"]
    quality = source["quality"]
    lifecycle = source["lifecycle"]
    lifecycle_summary = source["lifecycle_summary"]
    manifest = source["manifest"]
    checked_at = now or datetime.now(UTC)

    target_fields = (
        "target",
        "source",
        "succeeded",
        "references_discovered",
        "references_selected",
        "postings_extracted",
        "raw_records_saved",
        "posting_failures",
        "error",
    )
    event_fields = (
        "event_type",
        "detected_at",
        "target_name",
        "source_name",
        "employer",
        "title",
        "previous_status",
        "current_status",
    )
    targets = [
        {field: target.get(field) for field in target_fields}
        for target in collection.get("targets", [])
        if isinstance(target, dict)
    ]
    events = [
        {field: event.get(field) for field in event_fields}
        for event in lifecycle.get("events", [])
        if isinstance(event, dict)
    ]

    collection_fields = (
        "run_id",
        "profile_name",
        "query_names",
        "per_target_limit",
        "started_at",
        "finished_at",
        "targets_attempted",
        "targets_succeeded",
        "targets_failed",
        "search_attempts",
        "search_failures",
        "references_discovered",
        "references_selected",
        "postings_extracted",
        "unique_postings",
        "raw_records_saved",
        "posting_failures",
        "extraction_failure_rate",
    )
    collection_payload = {field: collection.get(field) for field in collection_fields}
    collection_payload["targets"] = targets

    quality_failed = not bool(quality.get("passed"))
    collection_failed = bool(collection.get("targets_failed"))
    lifecycle_failed = bool(lifecycle.get("failures"))
    gold_failed = bool(manifest.get("failures"))
    freshness = {
        "collection": _stage_freshness(collection.get("finished_at"), checked_at),
        "quality": _stage_freshness(quality.get("generated_at"), checked_at),
        "lifecycle": _stage_freshness(lifecycle.get("observed_at"), checked_at),
        "gold": _stage_freshness(manifest.get("generated_at"), checked_at),
    }
    stale = any(item["status"] != "fresh" for item in freshness.values())
    failed = quality_failed or collection_failed or lifecycle_failed or gold_failed
    status = "failed" if failed else "stale" if stale else "healthy"
    generated_values = [
        value
        for value in (
            collection.get("finished_at"),
            quality.get("generated_at"),
            lifecycle.get("observed_at"),
            manifest.get("generated_at"),
        )
        if isinstance(value, str)
    ]
    return {
        "status": status,
        "freshness": freshness,
        "generated_at": max(generated_values, default=None),
        "collection": collection_payload,
        "quality": quality,
        "lifecycle": {
            "run_id": lifecycle.get("run_id"),
            "collection_run_id": lifecycle.get("collection_run_id"),
            "observed_at": lifecycle.get("observed_at"),
            "already_applied": lifecycle.get("already_applied"),
            "tracked_postings": lifecycle.get("tracked_postings"),
            "current_counts": lifecycle.get("current_counts", {}),
            "event_counts": lifecycle.get("event_counts", {}),
            "events": events,
            "failures": lifecycle.get("failures", []),
            "employer_activity": lifecycle_summary.get("employer_activity", []),
        },
        "gold": manifest,
    }


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    """Serve dashboard assets and live reporting endpoints."""

    mart_path = DEFAULT_MART_PATH
    gold_root = DEFAULT_GOLD_ROOT
    static_dir = STATIC_DIR
    tracking_repository = ApplicationTrackingRepository(DEFAULT_TRACKING_PATH)
    allowed_hosts: ClassVar[frozenset[str]] = LOOPBACK_HOSTS
    resume_service: ClassVar[ResumeIngestionService | None] = None

    def __init__(
        self,
        request: socket | tuple[bytes, socket],
        client_address: tuple[str, int],
        server: BaseServer,
    ) -> None:
        """Serve static files from the dashboard asset directory."""
        super().__init__(
            request,
            client_address,
            server,
            directory=str(self.static_dir),
        )

    def _reject_foreign_request(self, *, mutating: bool) -> bool:
        """Block DNS-rebinding and cross-site requests; return True when rejected.

        The Host header must name an allowed local host so a hostile domain that
        re-resolves to 127.0.0.1 cannot read private dashboard data. State-changing
        requests must also come from an allowed Origin (when the browser sends one)
        and carry a JSON body, which a cross-site HTML form cannot produce.
        """
        if _hostname(self.headers.get("Host")) not in self.allowed_hosts:
            self._send_json({"error": "Host not allowed"}, status=HTTPStatus.FORBIDDEN)
            return True
        if not mutating:
            return False
        origin = self.headers.get("Origin")
        if origin is not None and _hostname(origin) not in self.allowed_hosts:
            self._send_json({"error": "Origin not allowed"}, status=HTTPStatus.FORBIDDEN)
            return True
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type.casefold() != "application/json":
            self._send_json(
                {"error": "Content-Type must be application/json"},
                status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
            return True
        return False

    def do_GET(self) -> None:  # noqa: N802
        """Route API requests and delegate asset requests to the static server."""
        if self._reject_foreign_request(mutating=False):
            return
        path = urlsplit(self.path).path
        if path == "/api/opportunities":
            try:
                mart = load_opportunities_mart(self.mart_path)
                self._send_json(attach_application_fit(mart, self._comparisons()))
            except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if path.startswith(COMPARISON_PREFIX):
            job_id = _path_identifier(path.removeprefix(COMPARISON_PREFIX))
            comparisons = self._comparisons()
            jobs = comparisons.get("jobs") if comparisons else None
            detail = jobs.get(job_id) if isinstance(jobs, dict) and job_id else None
            if detail is None:
                self._send_json(
                    {"error": "No comparison for this posting yet; run scripts/sync_data.py"},
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            self._send_json(
                {
                    "comparison": detail,
                    "generated_at": comparisons.get("generated_at") if comparisons else None,
                    "resume": comparisons.get("resume") if comparisons else None,
                    "model": comparisons.get("model") if comparisons else None,
                    "labels": self._labels_for(job_id) if job_id else {},
                }
            )
            return
        if path == LABELS_PATH:
            self._send_labels()
            return
        if path == LABEL_QUEUE_PATH:
            self._send_queue()
            return
        if path == LABEL_CHANGES_PATH:
            self._send_changes()
            return
        if path == "/api/application-tracking":
            try:
                self._send_json(self.tracking_repository.load().model_dump(mode="json"))
            except (ValueError, json.JSONDecodeError, ValidationError) as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if path == "/api/market-intelligence":
            try:
                self._send_json(load_market_intelligence(self.gold_root))
            except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if path == "/api/pipeline-operations":
            try:
                self._send_json(load_pipeline_operations(self.gold_root))
            except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if path == "/api/health":
            try:
                mart = load_opportunities_mart(self.mart_path)
                self._send_json(
                    {
                        "status": "ok",
                        "row_count": mart.get("row_count", 0),
                        "generated_at": mart.get("generated_at"),
                    }
                )
            except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
                self._send_json(
                    {"status": "error", "error": str(exc)},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
            return
        if path == "/api/candidate-profile":
            self._with_resume_service(load_candidate_profile)
            return
        if path == "/api/resume-reviews":
            self._with_resume_service(load_resume_reviews)
            return
        if path.startswith(RESUME_REVIEW_PREFIX):
            extraction_id = _path_identifier(path.removeprefix(RESUME_REVIEW_PREFIX))
            if extraction_id is None:
                self._send_json(
                    {"error": "A valid extraction ID is required"}, status=HTTPStatus.BAD_REQUEST
                )
                return
            self._with_resume_service(lambda service: load_resume_review(service, extraction_id))
            return
        if path.startswith("/api/"):
            self._send_json({"error": "Endpoint not found"}, status=HTTPStatus.NOT_FOUND)
            return
        if path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_PATCH(self) -> None:  # noqa: N802
        """Save an in-progress resume review without finalizing it."""
        if self._reject_foreign_request(mutating=True):
            return
        path = urlsplit(self.path).path
        if not path.startswith(RESUME_REVIEW_PREFIX):
            self._send_json({"error": "Endpoint not found"}, status=HTTPStatus.NOT_FOUND)
            return
        self._write_review(path.removeprefix(RESUME_REVIEW_PREFIX), ReviewDraft, approve=False)

    def do_PUT(self) -> None:  # noqa: N802
        """Approve a resume review, or persist one application-tracking record."""
        if self._reject_foreign_request(mutating=True):
            return
        path = urlsplit(self.path).path
        if path.startswith(RESUME_REVIEW_PREFIX) and path.endswith("/superseded"):
            raw_id = path.removeprefix(RESUME_REVIEW_PREFIX).removesuffix("/superseded")
            self._resume_action(raw_id, self._set_superseded)
            return
        if path == ACTIVE_RESUME_PATH:
            self._resume_action("active", self._activate_resume)
            return
        if path.startswith(RESUME_REVIEW_PREFIX):
            self._write_review(
                path.removeprefix(RESUME_REVIEW_PREFIX), ReviewApproval, approve=True
            )
            return
        prefix = "/api/application-tracking/"
        if not path.startswith(prefix):
            self._send_json({"error": "Endpoint not found"}, status=HTTPStatus.NOT_FOUND)
            return

        job_id = unquote(path.removeprefix(prefix)).strip()
        if not job_id or "/" in job_id or "\\" in job_id:
            self._send_json({"error": "A valid job ID is required"}, status=HTTPStatus.BAD_REQUEST)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
                raise ValueError("Request body must be between 1 byte and 64 KB")

            payload = json.loads(self.rfile.read(content_length))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")

            record = self.tracking_repository.update(job_id, payload)
            self._send_json(record.model_dump(mode="json"))

        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _comparisons(self) -> dict[str, object] | None:
        """Return stored Phase 6.3 comparison results, if any."""
        service = self.resume_service
        if service is None:
            return None
        try:
            return load_results(service.private_root)
        except (OSError, ValueError):
            return None

    def _label_store(self) -> RequirementLabelStore | None:
        """Return the private requirement-label log, when the resume workflow is set up."""
        service = self.resume_service
        return _shared_label_store(labels_path(service.private_root)) if service else None

    def _labels_for(self, job_id: str) -> dict[str, dict[str, object]]:
        """Return the owner's newest label per requirement line for one posting."""
        store = self._label_store()
        if store is None:
            return {}
        try:
            latest = store.latest_for(job_id)
        except (OSError, ValueError):
            return {}
        return {
            text: label.model_dump(
                mode="json",
                include={"predicted_status", "corrected_status", "agrees", "note", "labeled_at"},
            )
            for text, label in latest.items()
        }

    def _send_labels(self) -> None:
        """Send every newest label with its state against the latest comparison."""
        store = self._label_store()
        if store is None:
            self._send_json(
                {"error": "Resume workflow is unavailable"}, status=HTTPStatus.SERVICE_UNAVAILABLE
            )
            return
        try:
            labels = store.all()
        except (OSError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        comparisons = self._comparisons()
        rows = review_labels(labels, comparisons, self._postings())
        self._send_json(
            {
                "summary": summarize_labels(rows, Config().labels),
                "labels": rows,
                "comparison_generated_at": comparisons.get("generated_at") if comparisons else None,
                "comparison_model": comparisons.get("model") if comparisons else None,
            }
        )

    def _postings(self) -> dict[str, dict[str, Any]]:
        """Return mart rows by job id, for posting titles; empty before Gold is built."""
        try:
            mart = load_opportunities_mart(self.mart_path)
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return {}
        return {
            str(row["job_id"]): row
            for row in mart.get("opportunities", [])
            if isinstance(row, dict) and row.get("job_id")
        }

    def _send_queue(self) -> None:
        """Send review-queue progress and the next randomly drawn line to label."""
        store = self._label_store()
        if store is None:
            self._send_json(
                {"error": "Resume workflow is unavailable"}, status=HTTPStatus.SERVICE_UNAVAILABLE
            )
            return
        try:
            labels = store.all()
        except (OSError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        comparisons = self._comparisons()
        queue = build_queue(comparisons, labels, Config().labels, self._postings())
        self._send_json(
            {
                **public_queue(queue),
                "comparison_generated_at": comparisons.get("generated_at") if comparisons else None,
            }
        )

    def _tracking_statuses(self) -> list[str]:
        """Return every tracking decision's status, or none when tracking is unreadable."""
        try:
            records = self.tracking_repository.load().records
        except (OSError, ValueError, ValidationError):
            return []
        return [record.status.value for record in records.values()]

    def _send_changes(self) -> None:
        """Send the label change log, snapshots, and tracking-label progress."""
        store = self._label_store()
        if store is None:
            self._send_json(
                {"error": "Resume workflow is unavailable"}, status=HTTPStatus.SERVICE_UNAVAILABLE
            )
            return
        try:
            payload = build_changes(store, Config().labels, self._tracking_statuses())
        except (OSError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self._send_json(payload)

    def _save_snapshot(self) -> None:
        """Freeze the current label log as a snapshot (a no-op when nothing changed)."""
        store = self._label_store()
        if store is None:
            self._send_json(
                {"error": "Resume workflow is unavailable"}, status=HTTPStatus.SERVICE_UNAVAILABLE
            )
            return
        comparisons = self._comparisons()
        model = comparisons.get("model") if comparisons else None
        try:
            manifest, created = create_snapshot(
                store, Config().labels, model if isinstance(model, dict) else None
            )
        except (OSError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self._send_json(
            {"snapshot": manifest, "created": created},
            status=HTTPStatus.CREATED if created else HTTPStatus.OK,
        )

    def _record_queue_label(self) -> None:
        """Save a sampled label, but only for a line the sampler currently serves.

        The browser names the line it was shown; the server re-draws the queue and accepts
        the label only if that line is first in its group. That check is what makes the
        ``sampled`` flag trustworthy for Phase 7.
        """
        store = self._label_store()
        if store is None:
            self._send_json(
                {"error": "Resume workflow is unavailable"}, status=HTTPStatus.SERVICE_UNAVAILABLE
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
                raise ValueError("Request body must be between 1 byte and 64 KB")
            payload = json.loads(self.rfile.read(content_length))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            job_id = payload.get("job_id")
            requirement = payload.get("requirement")
            corrected = payload.get("corrected_status")
            note = payload.get("note")
            if not isinstance(job_id, str) or _path_identifier(job_id) is None:
                raise ValueError("job_id is required")
            if not isinstance(requirement, str) or not requirement:
                raise ValueError("requirement is required")
            if not isinstance(corrected, str):
                raise ValueError(VERDICT_ERROR)
            if note is not None and not isinstance(note, str):
                raise ValueError("note must be text")
            settings = Config().labels
            comparisons = self._comparisons()
            with store.lock:
                queue = build_queue(comparisons, store.all(), settings)
                if not is_queue_head(queue, job_id, requirement):
                    self._send_json(
                        {"error": "This line is no longer next in the review queue; refresh it"},
                        status=HTTPStatus.CONFLICT,
                    )
                    return
                label = store.record(
                    comparisons,
                    job_id,
                    requirement,
                    corrected,
                    source=LabelSource.QUEUE,
                    note=note,
                    sample_seed=settings.queue.seed,
                )
            self._send_json(label.model_dump(mode="json"), status=HTTPStatus.CREATED)
        except LookupError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as exc:
            self._send_json({"error": _validation_message(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _record_label(self) -> None:
        """Append the owner's verdict on one requirement line to the private label log."""
        store = self._label_store()
        if store is None:
            self._send_json(
                {"error": "Resume workflow is unavailable"}, status=HTTPStatus.SERVICE_UNAVAILABLE
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
                raise ValueError("Request body must be between 1 byte and 64 KB")
            payload = json.loads(self.rfile.read(content_length))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            job_id = payload.get("job_id")
            requirement = payload.get("requirement")
            action = payload.get("action", "label")
            raw_source = payload.get("source", LabelSource.DRAWER.value)
            note = payload.get("note")
            if not isinstance(job_id, str) or _path_identifier(job_id) is None:
                raise ValueError("job_id is required")
            if not isinstance(requirement, str) or not requirement:
                raise ValueError("requirement is required")
            if note is not None and not isinstance(note, str):
                raise ValueError("note must be text")
            # Queue labels and the sampled flag are assigned by the server, never claimed
            # by a request; only sampled labels may feed the Phase 7 accuracy estimate.
            if raw_source not in {source.value for source in CLIENT_SOURCES}:
                raise ValueError("source must be drawer or labels_tab")
            if "sampled" in payload or "sample_seed" in payload:
                raise ValueError("sampled is set by the server")
            source = LabelSource(raw_source)
            if action == "withdraw":
                label = store.withdraw(job_id, requirement, source=source, note=note)
            elif action == "label":
                corrected = payload.get("corrected_status")
                if not isinstance(corrected, str):
                    raise ValueError(VERDICT_ERROR)
                label = store.record(
                    self._comparisons(), job_id, requirement, corrected, source=source, note=note
                )
            else:
                raise ValueError("action must be label or withdraw")
            self._send_json(label.model_dump(mode="json"), status=HTTPStatus.CREATED)
        except LookupError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as exc:
            self._send_json({"error": _validation_message(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _with_resume_service(
        self, build: Callable[[ResumeIngestionService], dict[str, Any]]
    ) -> None:
        """Send ``build(service)`` as JSON, mapping repository errors to HTTP codes."""
        if self.resume_service is None:
            self._send_json(
                {"error": "Resume workflow is unavailable"}, status=HTTPStatus.SERVICE_UNAVAILABLE
            )
            return
        try:
            self._send_json(build(self.resume_service))
        except (ProfileRecordNotFoundError, LookupError, FileNotFoundError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)

    def _write_review(self, raw_id: str, model: type[BaseModel], *, approve: bool) -> None:
        """Validate and save a review draft or final approval for one extraction."""
        service = self.resume_service
        extraction_id = _path_identifier(raw_id)
        if service is None:
            self._send_json(
                {"error": "Resume workflow is unavailable"}, status=HTTPStatus.SERVICE_UNAVAILABLE
            )
            return
        if extraction_id is None:
            self._send_json(
                {"error": "A valid extraction ID is required"}, status=HTTPStatus.BAD_REQUEST
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_REVIEW_REQUEST_BYTES:
                raise ValueError("Review body must be between 1 byte and 512 KB")
            submission = model.model_validate_json(self.rfile.read(content_length))
            profile_id = service.profile().profile_id
            if approve:
                saved = service.repository.approve_review(
                    extraction_id, cast(ReviewApproval, submission), profile_id=profile_id
                )
            else:
                saved = service.repository.save_review_draft(
                    extraction_id, cast(ReviewDraft, submission), profile_id=profile_id
                )
            self._send_json(
                {
                    "extraction_id": saved.extraction_id,
                    "revision": saved.revision,
                    "review_status": saved.review_status.value,
                    "approved_sections": [section.value for section in saved.approved_sections],
                    "review_updated_at": saved.review_updated_at,
                    "approved_at": saved.approved_at,
                }
            )
        except ProfileRecordNotFoundError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except ProfileRepositoryConflictError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
        except (ValidationError, ValueError) as exc:
            self._send_json({"error": _validation_message(exc)}, status=HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:  # noqa: N802
        """Create a review revision, a resume version from edited text, or a requirement label."""
        if self._reject_foreign_request(mutating=True):
            return
        path = urlsplit(self.path).path
        if path.startswith(RESUME_REVIEW_PREFIX) and path.endswith("/revise"):
            raw_id = path.removeprefix(RESUME_REVIEW_PREFIX).removesuffix("/revise")
            self._resume_action(raw_id, self._revise_review)
            return
        if path.startswith(RESUME_VERSION_PREFIX) and path.endswith("/edited-text"):
            raw_id = path.removeprefix(RESUME_VERSION_PREFIX).removesuffix("/edited-text")
            self._resume_action(raw_id, self._edited_version)
            return
        if path == REQUIREMENT_LABELS_PATH:
            self._record_label()
            return
        if path == LABEL_QUEUE_PATH:
            self._record_queue_label()
            return
        if path == LABEL_SNAPSHOTS_PATH:
            self._save_snapshot()
            return
        self._send_json({"error": "Endpoint not found"}, status=HTTPStatus.NOT_FOUND)

    def _resume_action(
        self,
        raw_id: str,
        action: Callable[[ResumeIngestionService, str, dict[str, Any]], dict[str, Any]],
    ) -> None:
        """Run one resume workflow action on a JSON body, mapping errors to HTTP codes."""
        service = self.resume_service
        if service is None:
            self._send_json(
                {"error": "Resume workflow is unavailable"}, status=HTTPStatus.SERVICE_UNAVAILABLE
            )
            return
        identifier = _path_identifier(raw_id)
        if identifier is None:
            self._send_json({"error": "A valid ID is required"}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_REVIEW_REQUEST_BYTES:
                raise ValueError("Request body must be between 1 byte and 512 KB")
            payload = json.loads(self.rfile.read(content_length))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            self._send_json(action(service, identifier, payload))
        except (ProfileRecordNotFoundError, LookupError, FileNotFoundError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except ProfileRepositoryConflictError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
        except (ResumeDocumentError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    @staticmethod
    def _set_superseded(
        service: ResumeIngestionService, extraction_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        superseded = payload.get("superseded")
        if not isinstance(superseded, bool):
            raise ValueError("superseded must be true or false")
        saved = service.repository.set_superseded(
            extraction_id, superseded, profile_id=service.profile().profile_id
        )
        return {
            "extraction_id": saved.extraction_id,
            "superseded": saved.superseded,
            "revision": saved.revision,
        }

    @staticmethod
    def _revise_review(
        service: ResumeIngestionService, extraction_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        revision = service.revise(extraction_id)
        return {"extraction_id": revision.extraction_id, "revised_from": extraction_id}

    @staticmethod
    def _edited_version(
        service: ResumeIngestionService, resume_version_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        text = payload.get("text")
        label = payload.get("label")
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        if label is not None and not isinstance(label, str):
            raise ValueError("label must be a string")
        result = service.create_edited_version(resume_version_id, text, label=label or None)
        return result.summary()

    @staticmethod
    def _activate_resume(
        service: ResumeIngestionService, _: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        resume_version_id = payload.get("resume_version_id")
        if not isinstance(resume_version_id, str) or _path_identifier(resume_version_id) is None:
            raise ValueError("resume_version_id is required")
        version = service.activate_approved(resume_version_id)
        return {"active_resume_version_id": version.resume_version_id}

    def _send_json(
        self,
        payload: object,
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        """Write one UTF-8 JSON response."""
        body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        """Prevent stale assets, MIME sniffing, and framing by other sites."""
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        """Keep routine local asset requests out of application logs."""
        return


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    mart_path: Path = DEFAULT_MART_PATH,
    static_dir: Path = STATIC_DIR,
    tracking_path: Path = DEFAULT_TRACKING_PATH,
    gold_root: Path = DEFAULT_GOLD_ROOT,
    private_root: Path | None = None,
) -> DashboardHTTPServer:
    """Create a configured threaded dashboard server."""
    allowed_hosts = LOOPBACK_HOSTS | {host.strip("[]").casefold()}
    resume_service = ResumeIngestionService(Config(), private_root=private_root)
    profile_id = resume_service.profile().profile_id

    def known_resume_versions() -> set[str]:
        return {
            version.resume_version_id
            for version in resume_service.repository.list_resume_versions(profile_id)
        }

    handler = type(
        "ConfiguredDashboardRequestHandler",
        (DashboardRequestHandler,),
        {
            "mart_path": mart_path,
            "gold_root": gold_root,
            "static_dir": static_dir,
            "tracking_repository": ApplicationTrackingRepository(
                tracking_path, profile_id=profile_id, known_resume_versions=known_resume_versions
            ),
            "allowed_hosts": frozenset(allowed_hosts),
            "resume_service": resume_service,
        },
    )
    return DashboardHTTPServer((host, port), handler)


def attach_application_fit(
    mart: dict[str, Any], comparisons: dict[str, object] | None
) -> dict[str, Any]:
    """Add each posting's Phase 6.3 fit summary (no resume evidence) to the mart."""
    jobs = comparisons.get("jobs") if comparisons else None
    if not isinstance(jobs, dict):
        return {**mart, "application_fit_available": False}
    rows = []
    for row in mart.get("opportunities", []):
        detail = jobs.get(row.get("job_id"))
        if isinstance(detail, dict):
            dimensions = detail.get("dimensions") or {}
            requirements = dimensions.get("requirements") or {}
            keywords = dimensions.get("keywords") or {}
            row = {
                **row,
                "application_fit": {
                    "fit_score": detail.get("fit_score"),
                    "band": detail.get("band"),
                    "requirements_score": requirements.get("score"),
                    "required_met": requirements.get("required_met"),
                    "required_total": requirements.get("required_total"),
                    "keywords_score": keywords.get("score"),
                },
            }
        rows.append(row)
    return {
        **mart,
        "opportunities": rows,
        "application_fit_available": True,
        "application_fit_generated_at": comparisons.get("generated_at") if comparisons else None,
    }


_LABEL_STORES: dict[Path, RequirementLabelStore] = {}
_LABEL_STORES_LOCK = Lock()


def _shared_label_store(path: Path) -> RequirementLabelStore:
    """Return one store per log file, so its lock is shared by every request thread."""
    with _LABEL_STORES_LOCK:
        store = _LABEL_STORES.get(path)
        if store is None:
            store = _LABEL_STORES[path] = RequirementLabelStore(path)
        return store


def _path_identifier(raw: str) -> str | None:
    """Decode one URL path identifier, rejecting nested paths and traversal."""
    value = unquote(raw).strip()
    if not value or "/" in value or "\\" in value or value in {".", ".."} or len(value) > 200:
        return None
    return value


def _validation_message(exc: Exception) -> str:
    """Return a short, readable validation error."""
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first.get("loc", ()))
        return (
            f"{location}: {first.get('msg', 'invalid value')}"
            if location
            else str(first.get("msg"))
        )
    return str(exc)


def build_parser() -> argparse.ArgumentParser:
    """Build command-line arguments for the local dashboard server."""
    parser = argparse.ArgumentParser(description="Serve the JMIP opportunities dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--mart", type=Path, help="Opportunities mart (default: in the configured Gold folder)"
    )
    parser.add_argument("--tracking", type=Path, default=DEFAULT_TRACKING_PATH)
    parser.add_argument(
        "--gold-root",
        type=Path,
        help="Gold folder (default: settings, or JMIP_PIPELINE_DATA_DIR/gold when set)",
    )
    return parser


def configured_gold_root() -> Path:
    """Return the Gold folder from settings, honoring JMIP_PIPELINE_DATA_DIR."""
    return Path(Config().settings.paths.gold_dir)


def main(argv: list[str] | None = None) -> int:
    """Run the dashboard until interrupted."""
    args = build_parser().parse_args(argv)
    gold_root = args.gold_root or configured_gold_root()
    mart_path = args.mart or gold_root / "current_opportunities" / "mart.json"
    try:
        server = create_server(
            args.host,
            args.port,
            mart_path=mart_path,
            tracking_path=args.tracking,
            gold_root=gold_root,
        )
    except OSError as exc:
        print(
            f"Unable to start JMIP dashboard on http://{args.host}:{args.port}: {exc}. "
            "Stop the existing dashboard or choose another --port."
        )
        return 1
    print(f"JMIP dashboard available at http://{args.host}:{server.server_port}")
    print(f"Reading Gold data from {gold_root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
