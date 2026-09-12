"""Local server for the Job Market Intelligence dashboard."""

from __future__ import annotations

import argparse
import json
import socket as socket_module
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socket import socket
from socketserver import BaseServer
from threading import RLock
from typing import Any, cast
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dashboard.data_management import DataManagementService
from dashboard.pipeline_control import (
    PipelineAction,
    PipelineOperationsController,
    PipelineRunConflictError,
)
from dashboard.tracking import ApplicationTrackingRepository
from models import ResumeExtractionReviewDraft, ResumeExtractionReviewSubmission
from profiles import (
    CandidateProfileRepository,
    DefaultProfileMigrator,
    DeterministicApplicationComparisonService,
    ProfileRecordNotFoundError,
    ProfileRepositoryConflictError,
)
from reporting import ScheduledReportingService, load_reporting_dashboard
from utils.config import Config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_MART_PATH = PROJECT_ROOT / "data" / "gold" / "current_opportunities" / "mart.json"
DEFAULT_TRACKING_PATH = PROJECT_ROOT / "data" / "application_tracking" / "state.json"
DEFAULT_GOLD_ROOT = PROJECT_ROOT / "data" / "gold"
DEFAULT_REPORT_ROOT = PROJECT_ROOT / "data" / "reports"
DEFAULT_ALERT_STATE_PATH = PROJECT_ROOT / "data" / "alerts" / "state.json"
DEFAULT_OPERATIONS_STATE_PATH = PROJECT_ROOT / "data" / "operations" / "dashboard_runs.json"
DEFAULT_BACKUP_ROOT = PROJECT_ROOT / "data" / "backups"
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
OPERATIONS_FRESHNESS_HOURS = 36


class ApplicationComparisonRequest(BaseModel):
    """Dashboard request for comparing one approved resume to one opportunity."""

    model_config = ConfigDict(extra="forbid")

    resume_version_id: str = Field(min_length=1, max_length=200)


class PipelineRunRequest(BaseModel):
    """One allowlisted pipeline action requested from the local dashboard."""

    model_config = ConfigDict(extra="forbid")

    action: PipelineAction


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


def load_candidate_profile(
    repository: CandidateProfileRepository,
    profile_id: str,
) -> dict[str, Any]:
    """Return the active profile and a safe projection of its resume versions."""
    profile = repository.get_profile(profile_id)
    versions = repository.list_resume_versions(profile_id)
    return {
        "profile": profile.model_dump(mode="json"),
        "resume_versions": [
            {
                "resume_version_id": version.resume_version_id,
                "version_label": version.version_label,
                "media_type": version.media_type.value,
                "status": version.status.value,
                "uploaded_at": version.uploaded_at.isoformat(),
                "is_active": version.resume_version_id == profile.active_resume_version_id,
            }
            for version in versions
        ],
    }


def load_resume_reviews(
    repository: CandidateProfileRepository,
    profile_id: str,
) -> dict[str, Any]:
    """Return review-safe structured facts without private artifact metadata."""
    profile = repository.get_profile(profile_id)
    versions = repository.list_resume_versions(profile_id)
    items: list[dict[str, Any]] = []
    resumes_without_extraction = 0
    for version in versions:
        extractions = repository.list_extractions(version.resume_version_id)
        if not extractions:
            resumes_without_extraction += 1
        for extraction in extractions:
            section_counts = {
                "skills": len(extraction.skills),
                "employment": len(extraction.work_experience),
                "education": len(extraction.education),
                "projects": len(extraction.projects),
            }
            populated_sections = sum(count > 0 for count in section_counts.values())
            items.append(
                {
                    "resume_version": {
                        "resume_version_id": version.resume_version_id,
                        "version_label": version.version_label,
                        "media_type": version.media_type.value,
                        "status": version.status.value,
                        "uploaded_at": version.uploaded_at.isoformat(),
                        "is_active": (
                            version.resume_version_id == profile.active_resume_version_id
                        ),
                    },
                    "extraction": {
                        "extraction_id": extraction.extraction_id,
                        "resume_version_id": extraction.resume_version_id,
                        "extractor_name": extraction.extractor_name,
                        "extractor_version": extraction.extractor_version,
                        "status": extraction.status.value,
                        "review_status": extraction.review_status.value,
                        "page_count": extraction.page_count,
                        "character_count": extraction.character_count,
                        "language": extraction.language,
                        "quality_score": extraction.quality_score,
                        "structured_completeness": {
                            "score": populated_sections / len(section_counts),
                            "populated_sections": populated_sections,
                            "total_sections": len(section_counts),
                            "fact_count": sum(section_counts.values()),
                            "section_counts": section_counts,
                            "approval_progress": (
                                len(extraction.reviewed_sections) / len(section_counts)
                            ),
                        },
                        "skills": [item.model_dump(mode="json") for item in extraction.skills],
                        "work_experience": [
                            item.model_dump(mode="json") for item in extraction.work_experience
                        ],
                        "education": [
                            item.model_dump(mode="json") for item in extraction.education
                        ],
                        "projects": [
                            item.model_dump(mode="json") for item in extraction.projects
                        ],
                        "warnings": extraction.warnings,
                        "quality_checks": [
                            check.model_dump(mode="json") for check in extraction.quality_checks
                        ],
                        "reviewed_sections": [
                            section.value for section in extraction.reviewed_sections
                        ],
                        "review_comment": extraction.review_comment,
                        "extracted_at": extraction.extracted_at.isoformat(),
                        "review_updated_at": (
                            extraction.review_updated_at.isoformat()
                            if extraction.review_updated_at is not None
                            else None
                        ),
                        "reviewed_at": (
                            extraction.reviewed_at.isoformat()
                            if extraction.reviewed_at is not None
                            else None
                        ),
                        "can_review": (
                            extraction.review_status.value == "pending"
                            and extraction.status.value != "failed"
                        ),
                    },
                }
            )
    return {
        "profile_id": profile.profile_id,
        "items": items,
        "pending_count": sum(
            item["extraction"]["review_status"] == "pending"
            for item in items
        ),
        "approved_count": sum(
            item["extraction"]["review_status"] == "approved"
            for item in items
        ),
        "resumes_without_extraction": resumes_without_extraction,
    }


def load_application_comparisons(
    repository: CandidateProfileRepository,
    profile_id: str,
    job_id: str,
) -> dict[str, Any]:
    """Return persisted comparisons for one job without private resume content."""
    runs = [
        run
        for run in repository.list_analysis_runs(profile_id)
        if run.mode.value == "application" and run.job_id == job_id
    ]
    return {
        "job_id": job_id,
        "comparisons": [run.model_dump(mode="json") for run in runs],
        "latest": runs[0].model_dump(mode="json") if runs else None,
    }


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    """Serve dashboard assets and live reporting endpoints."""

    mart_path = DEFAULT_MART_PATH
    gold_root = DEFAULT_GOLD_ROOT
    report_root = DEFAULT_REPORT_ROOT
    alert_state_path = DEFAULT_ALERT_STATE_PATH
    static_dir = STATIC_DIR
    tracking_repository = ApplicationTrackingRepository(DEFAULT_TRACKING_PATH)
    profile_repository: CandidateProfileRepository | None = None
    default_profile_id = "profile-default"
    reporting_lock = RLock()
    pipeline_controller: PipelineOperationsController | None = None
    data_management_service: DataManagementService | None = None
    data_management_lock = RLock()

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

    def do_GET(self) -> None:  # noqa: N802
        """Route API requests and delegate asset requests to the static server."""
        path = urlsplit(self.path).path
        if path == "/api/opportunities":
            self._send_json(load_opportunities_mart(self.mart_path))
            return
        if path == "/api/application-tracking":
            self._send_json(self.tracking_repository.load().model_dump(mode="json"))
            return
        if path == "/api/candidate-profile":
            if self.profile_repository is None:
                self._send_json(
                    {"error": "Candidate profile repository is unavailable"},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
            else:
                self._send_json(
                    load_candidate_profile(
                        self.profile_repository,
                        self.default_profile_id,
                    )
                )
            return
        if path == "/api/resume-reviews":
            if self.profile_repository is None:
                self._send_json(
                    {"error": "Candidate profile repository is unavailable"},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
            else:
                self._send_json(
                    load_resume_reviews(
                        self.profile_repository,
                        self.default_profile_id,
                    )
                )
            return
        comparison_prefix = "/api/application-comparisons/"
        if path.startswith(comparison_prefix):
            self._get_application_comparisons(path.removeprefix(comparison_prefix))
            return
        if path == "/api/market-intelligence":
            try:
                self._send_json(load_market_intelligence(self.gold_root))
            except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
                self._send_json(
                    {"error": str(exc)},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
            return
        if path == "/api/pipeline-operations":
            try:
                payload = load_pipeline_operations(self.gold_root)
                payload["control"] = (
                    self.pipeline_controller.snapshot()
                    if self.pipeline_controller is not None
                    else {"active_run_id": None, "can_start": False, "runs": []}
                )
                self._send_json(payload)
            except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
                self._send_json(
                    {"error": str(exc)},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
            return
        if path == "/api/data-management":
            if self.data_management_service is None:
                self._send_json(
                    {"error": "Data management is unavailable"},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
            else:
                try:
                    self._send_json(self.data_management_service.inventory())
                except (OSError, ValueError) as exc:
                    self._send_json(
                        {"error": str(exc)},
                        status=HTTPStatus.SERVICE_UNAVAILABLE,
                    )
            return
        snapshot_prefix = "/api/data-snapshots/"
        if path.startswith(snapshot_prefix):
            self._download_data_snapshot(path.removeprefix(snapshot_prefix))
            return
        if path == "/api/reporting":
            try:
                self._send_json(
                    load_reporting_dashboard(
                        self.report_root,
                        self.alert_state_path,
                        Config().reporting,
                    )
                )
            except (ValueError, json.JSONDecodeError, ValidationError) as exc:
                self._send_json(
                    {"error": str(exc)},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
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
        if path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_PUT(self) -> None:  # noqa: N802
        """Validate and persist one candidate-controlled dashboard record."""
        path = urlsplit(self.path).path
        review_prefix = "/api/resume-reviews/"
        if path.startswith(review_prefix):
            self._approve_resume_review(path.removeprefix(review_prefix))
            return

        prefix = "/api/application-tracking/"

        if not path.startswith(prefix):
            self._send_json({"error": "Endpoint not found"}, status=HTTPStatus.NOT_FOUND)
            return

        job_id = unquote(path.removeprefix(prefix)).strip()
        if not job_id or "/" in job_id:
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

    def do_PATCH(self) -> None:  # noqa: N802
        """Persist an in-progress candidate review without publishing it."""
        path = urlsplit(self.path).path
        review_prefix = "/api/resume-reviews/"
        if not path.startswith(review_prefix):
            self._send_json({"error": "Endpoint not found"}, status=HTTPStatus.NOT_FOUND)
            return
        self._save_resume_review_draft(path.removeprefix(review_prefix))

    def _save_resume_review_draft(self, raw_extraction_id: str) -> None:
        """Save corrected facts and independently approved review sections."""
        if self.profile_repository is None:
            self._send_json(
                {"error": "Candidate profile repository is unavailable"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        extraction_id = unquote(raw_extraction_id).strip()
        if not extraction_id or "/" in extraction_id:
            self._send_json(
                {"error": "A valid extraction ID is required"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_REVIEW_REQUEST_BYTES:
                raise ValueError("Review body must be between 1 byte and 512 KB")
            payload = json.loads(self.rfile.read(content_length))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            draft = ResumeExtractionReviewDraft.model_validate(payload)
            saved = self.profile_repository.save_extraction_review_draft(
                extraction_id,
                draft,
                profile_id=self.default_profile_id,
            )
            self._send_json(
                {
                    "extraction_id": saved.extraction_id,
                    "review_status": saved.review_status.value,
                    "reviewed_sections": [
                        section.value for section in saved.reviewed_sections
                    ],
                    "review_updated_at": saved.review_updated_at,
                }
            )
        except ProfileRecordNotFoundError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except ProfileRepositoryConflictError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _approve_resume_review(self, raw_extraction_id: str) -> None:
        """Approve one complete, candidate-reviewed structured extraction."""
        if self.profile_repository is None:
            self._send_json(
                {"error": "Candidate profile repository is unavailable"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        extraction_id = unquote(raw_extraction_id).strip()
        if not extraction_id or "/" in extraction_id:
            self._send_json(
                {"error": "A valid extraction ID is required"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_REVIEW_REQUEST_BYTES:
                raise ValueError("Review body must be between 1 byte and 512 KB")
            payload = json.loads(self.rfile.read(content_length))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            submission = ResumeExtractionReviewSubmission.model_validate(payload)
            approved = self.profile_repository.approve_extraction_review(
                extraction_id,
                submission,
                profile_id=self.default_profile_id,
            )
            self._send_json(
                {
                    "extraction_id": approved.extraction_id,
                    "review_status": approved.review_status.value,
                    "reviewed_sections": [
                        section.value for section in approved.reviewed_sections
                    ],
                    "reviewed_at": approved.reviewed_at,
                }
            )
        except ProfileRecordNotFoundError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except ProfileRepositoryConflictError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:  # noqa: N802
        """Run safe, local dashboard actions that do not perform live extraction."""
        path = urlsplit(self.path).path
        if path == "/api/pipeline-runs":
            self._start_pipeline_run()
            return
        if path == "/api/data-snapshots":
            self._create_data_snapshot()
            return
        comparison_prefix = "/api/application-comparisons/"
        if path.startswith(comparison_prefix):
            self._run_application_comparison(path.removeprefix(comparison_prefix))
            return
        if path != "/api/reporting/generate":
            self._send_json({"error": "Endpoint not found"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            with self.reporting_lock:
                result = ScheduledReportingService(
                    Config(),
                    gold_root=self.gold_root,
                    tracking_path=self.tracking_repository.path,
                    report_root=self.report_root,
                    alert_state_path=self.alert_state_path,
                ).run()
            self._send_json(result.summary(), status=HTTPStatus.CREATED)
        except (FileNotFoundError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            self._send_json(
                {"error": str(exc)},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )

    def _start_pipeline_run(self) -> None:
        """Queue one predefined pipeline operation without blocking HTTP requests."""
        if self.pipeline_controller is None:
            self._send_json(
                {"error": "Pipeline controls are unavailable"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
                raise ValueError("Request body must be between 1 byte and 64 KB")
            payload = json.loads(self.rfile.read(content_length))
            request = PipelineRunRequest.model_validate(payload)
            run = self.pipeline_controller.start(request.action)
            self._send_json(run.model_dump(mode="json"), status=HTTPStatus.ACCEPTED)
        except PipelineRunConflictError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _create_data_snapshot(self) -> None:
        """Create one non-private recovery snapshot from fixed project roots."""
        if self.data_management_service is None:
            self._send_json(
                {"error": "Data management is unavailable"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if self.pipeline_controller is not None:
            control = self.pipeline_controller.snapshot()
            if control["active_run_id"] is not None:
                self._send_json(
                    {"error": "Wait for the active pipeline operation before creating a snapshot"},
                    status=HTTPStatus.CONFLICT,
                )
                return
        try:
            with self.data_management_lock:
                snapshot = self.data_management_service.create_snapshot()
            self._send_json(snapshot.model_dump(mode="json"), status=HTTPStatus.CREATED)
        except (OSError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.SERVICE_UNAVAILABLE)

    def _download_data_snapshot(self, raw_filename: str) -> None:
        """Download one catalogued snapshot without exposing arbitrary files."""
        if self.data_management_service is None:
            self._send_json(
                {"error": "Data management is unavailable"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        filename = unquote(raw_filename).strip()
        if not filename or "/" in filename or "\\" in filename:
            self._send_json(
                {"error": "A valid snapshot filename is required"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            path = self.data_management_service.snapshot_path(filename)
        except FileNotFoundError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            return
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                self.wfile.write(chunk)

    def _get_application_comparisons(self, raw_job_id: str) -> None:
        """Return saved comparison runs for one canonical opportunity."""
        if self.profile_repository is None:
            self._send_json(
                {"error": "Candidate profile repository is unavailable"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        job_id = unquote(raw_job_id).strip()
        if not job_id or "/" in job_id:
            self._send_json(
                {"error": "A valid job ID is required"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        self._send_json(
            load_application_comparisons(
                self.profile_repository,
                self.default_profile_id,
                job_id,
            )
        )

    def _run_application_comparison(self, raw_job_id: str) -> None:
        """Compare one approved resume extraction to an exact Gold mart row."""
        if self.profile_repository is None:
            self._send_json(
                {"error": "Candidate profile repository is unavailable"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        job_id = unquote(raw_job_id).strip()
        if not job_id or "/" in job_id:
            self._send_json(
                {"error": "A valid job ID is required"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
                raise ValueError("Request body must be between 1 byte and 64 KB")
            payload = json.loads(self.rfile.read(content_length))
            request = ApplicationComparisonRequest.model_validate(payload)
            mart = load_opportunities_mart(self.mart_path)
            opportunity = next(
                (
                    item
                    for item in mart["opportunities"]
                    if isinstance(item, dict) and item.get("job_id") == job_id
                ),
                None,
            )
            if opportunity is None:
                raise ProfileRecordNotFoundError(f"opportunity not found: {job_id}")
            run = DeterministicApplicationComparisonService(
                self.profile_repository,
                Config().settings.application_comparison,
            ).compare(
                profile_id=self.default_profile_id,
                resume_version_id=request.resume_version_id,
                opportunity=opportunity,
                gold_input_fingerprint=mart.get("input_fingerprint"),
            )
            self._send_json(run.model_dump(mode="json"), status=HTTPStatus.CREATED)
        except ProfileRecordNotFoundError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except ProfileRepositoryConflictError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

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
        """Prevent stale local assets and API payloads during active development."""
        self.send_header("Cache-Control", "no-store")
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
    report_root: Path = DEFAULT_REPORT_ROOT,
    alert_state_path: Path = DEFAULT_ALERT_STATE_PATH,
    operations_state_path: Path = DEFAULT_OPERATIONS_STATE_PATH,
    backup_root: Path | None = None,
    data_project_root: Path = PROJECT_ROOT,
    profile_database: Path | None = None,
    pipeline_controller: PipelineOperationsController | None = None,
) -> DashboardHTTPServer:
    """Create a configured threaded dashboard server."""
    config = Config()
    profile_repository = CandidateProfileRepository(
        profile_database or config.settings.paths.profile_database
    )
    migration = DefaultProfileMigrator(profile_repository, config).migrate()
    handler = type(
        "ConfiguredDashboardRequestHandler",
        (DashboardRequestHandler,),
        {
            "mart_path": mart_path,
            "gold_root": gold_root,
            "report_root": report_root,
            "alert_state_path": alert_state_path,
            "static_dir": static_dir,
            "profile_repository": profile_repository,
            "default_profile_id": migration.profile.profile_id,
            "tracking_repository": ApplicationTrackingRepository(
                tracking_path,
                profile_repository=profile_repository,
                profile_id=migration.profile.profile_id,
            ),
            "pipeline_controller": pipeline_controller
            or PipelineOperationsController(PROJECT_ROOT, operations_state_path),
            "data_management_service": DataManagementService(data_project_root, backup_root),
        },
    )
    return DashboardHTTPServer((host, port), handler)


def build_parser() -> argparse.ArgumentParser:
    """Build command-line arguments for the local dashboard server."""
    parser = argparse.ArgumentParser(description="Serve the JMIP opportunities dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mart", type=Path, default=DEFAULT_MART_PATH)
    parser.add_argument("--tracking", type=Path, default=DEFAULT_TRACKING_PATH)
    parser.add_argument("--gold-root", type=Path, default=DEFAULT_GOLD_ROOT)
    parser.add_argument("--report-root", type=Path, default=DEFAULT_REPORT_ROOT)
    parser.add_argument("--alert-state", type=Path, default=DEFAULT_ALERT_STATE_PATH)
    parser.add_argument("--operations-state", type=Path, default=DEFAULT_OPERATIONS_STATE_PATH)
    parser.add_argument("--backup-root", type=Path)
    parser.add_argument("--profile-database", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the dashboard until interrupted."""
    args = build_parser().parse_args(argv)
    try:
        server = create_server(
            args.host,
            args.port,
            mart_path=args.mart,
            tracking_path=args.tracking,
            gold_root=args.gold_root,
            report_root=args.report_root,
            alert_state_path=args.alert_state,
            operations_state_path=args.operations_state,
            backup_root=args.backup_root,
            profile_database=args.profile_database,
        )
    except OSError as exc:
        print(
            f"Unable to start JMIP dashboard on http://{args.host}:{args.port}: {exc}. "
            "Stop the existing dashboard or choose another --port."
        )
        return 1
    print(f"JMIP dashboard available at http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
