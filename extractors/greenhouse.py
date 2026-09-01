"""Greenhouse Job Board API extractor."""

from __future__ import annotations

import copy
import html
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from extractors.base_extractor import BaseExtractor
from models.job_posting import (
    CompanyInfo,
    DescriptionInfo,
    EmploymentType,
    JobPosting,
    LocationInfo,
    PositionInfo,
    PostingMetadata,
    PostingStatus,
    RawSourceRecord,
    SourceInfo,
    SourceType,
    WorkplaceType,
)
from storage.base import BaseStorage
from utils.checkpoint import CheckpointManager
from utils.config import Config
from utils.hashing import stable_json_hash
from utils.logger import log_event


class GreenhouseResponseError(RuntimeError):
    """Raised when Greenhouse returns a malformed public API response."""


class GreenhouseExtractor(BaseExtractor):
    """Extract public postings from one Greenhouse job board.

    Greenhouse's public Job Board API lists all jobs for a board rather than
    providing server-side search. This implementation requests full job content
    once, filters locally, and caches matching payloads for ``fetch_job``.
    """

    source_name = "greenhouse"

    def __init__(
        self,
        board_token: str,
        *,
        company_name: str | None = None,
        config: Config | None = None,
        bronze_storage: BaseStorage | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize extraction for a Greenhouse board token."""
        token = board_token.strip()
        if not token:
            raise ValueError("board_token must not be empty")

        super().__init__(
            self.source_name,
            config=config,
            bronze_storage=bronze_storage,
            checkpoint_manager=checkpoint_manager,
        )
        source = self.source_definition
        if source is None:
            raise ValueError("Missing greenhouse source configuration")
        if not source.enabled:
            raise ValueError("The greenhouse source is disabled")

        self.board_token = token
        self.company_name = company_name.strip() if company_name else None
        self.api_base_url = (source.api_base_url or source.base_url).rstrip("/")
        self._sleep = sleep
        self._monotonic = monotonic
        self._minimum_request_interval = 60.0 / source.rate_limit_per_minute
        self._last_request_at: float | None = None
        self._job_cache: dict[str, dict[str, Any]] = {}
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=self.config.settings.scraping.request_timeout_seconds,
            headers={"User-Agent": self.config.settings.scraping.user_agent},
            follow_redirects=True,
        )

    def close(self) -> None:
        """Close the internally owned HTTP client."""
        if self._owns_client:
            self._client.close()

    @property
    def checkpoint_name(self) -> str:
        """Return a checkpoint name scoped to this Greenhouse board."""
        return f"{self.source_name}-{self.board_token}-extractor"

    def __enter__(self) -> GreenhouseExtractor:
        """Return this extractor as a context manager."""
        return self

    def __exit__(self, *_args: object) -> None:
        """Release the internally owned HTTP client."""
        self.close()

    def search_jobs(
        self,
        query: str,
        location: str | None = None,
        **filters: object,
    ) -> list[str]:
        """List the board's jobs and return references matching local filters.

        Supported filters are ``department``, ``office``, and ``language``.
        Values are matched case-insensitively as substrings.
        """
        supported_filters = {"department", "office", "language"}
        unknown_filters = set(filters) - supported_filters
        if unknown_filters:
            names = ", ".join(sorted(unknown_filters))
            raise ValueError(f"Unsupported Greenhouse search filters: {names}")

        if self.company_name is None:
            board = self._request_json(self._board_url)
            name = board.get("name")
            if not isinstance(name, str) or not name.strip():
                raise GreenhouseResponseError("Greenhouse board response is missing a name")
            self.company_name = name.strip()

        payload = self._request_json(self._jobs_url, params={"content": "true"})
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            raise GreenhouseResponseError("Greenhouse jobs response is missing a jobs list")

        self._job_cache.clear()
        references: list[str] = []
        for item in jobs:
            if not isinstance(item, dict) or item.get("id") is None:
                log_event(
                    "WARNING",
                    "Skipping malformed Greenhouse job summary",
                    component="extractor",
                    event="greenhouse_malformed_job",
                    source=self.source_name,
                )
                continue
            raw_job = dict(item)
            if not self._matches(raw_job, query, location, filters):
                continue
            reference = str(raw_job["id"])
            self._job_cache[reference] = raw_job
            references.append(reference)

        log_event(
            "INFO",
            "Greenhouse search matched {count} postings",
            component="extractor",
            event="greenhouse_search_complete",
            source=self.source_name,
            board_token=self.board_token,
            count=len(references),
        )
        return references

    def fetch_job(self, job_reference: str) -> dict[str, Any]:
        """Return a cached job or retrieve it from Greenhouse by post ID."""
        cached = self._job_cache.get(str(job_reference))
        if cached is not None:
            return copy.deepcopy(cached)
        return self._request_json(f"{self._jobs_url}/{quote(str(job_reference), safe='')}")

    def parse_job(self, raw_job: dict[str, Any]) -> JobPosting:
        """Convert a Greenhouse API payload into the canonical job model."""
        external_id = self._required_text(raw_job, "id")
        title = self._required_text(raw_job, "title")
        absolute_url = self._optional_text(raw_job.get("absolute_url"))
        location_raw = self._nested_text(raw_job, "location", "name")
        decoded_html = self._decode_content(self._optional_text(raw_job.get("content")))
        clean_text = self._html_to_text(decoded_html)
        departments = self._named_values(raw_job.get("departments"))
        offices = self._named_values(raw_job.get("offices"))

        posting = JobPosting(
            metadata=PostingMetadata(
                job_id="",
                status=PostingStatus.ACTIVE,
                tags=[*departments, *offices],
            ),
            source=SourceInfo.model_validate(
                {
                    "name": self.source_name,
                    "source_type": SourceType.ATS,
                    "url": absolute_url,
                    "external_id": external_id,
                    "fetched_at": datetime.now(UTC),
                }
            ),
            company=CompanyInfo(name=self.company_name or self.board_token),
            position=PositionInfo(
                title=title,
                family=departments[0] if departments else None,
            ),
            location=LocationInfo(
                raw=location_raw,
                workplace_type=self._workplace_type(location_raw),
            ),
            employment_type=self._employment_type(raw_job),
            description=DescriptionInfo(
                raw_text=clean_text,
                clean_text=clean_text,
                html=decoded_html,
            ),
            raw=RawSourceRecord.model_validate(
                {
                    "raw_url": absolute_url,
                    "raw_payload": raw_job,
                    "raw_text": self._optional_text(raw_job.get("content")),
                }
            ),
        )
        return posting.with_derived_metadata()

    def raw_storage_key(self, raw_job: dict[str, Any]) -> str:
        """Namespace immutable raw versions by board, job ID, and payload hash."""
        external_id = self._required_text(raw_job, "id")
        version = stable_json_hash(raw_job)
        return f"{self.source_name}/{self.board_token}/{external_id}/{version}"

    def save_raw(self, raw_job: dict[str, Any]) -> Path | None:
        """Save a new raw version and treat an identical snapshot as idempotent."""
        if self.bronze_storage is None:
            raise RuntimeError("bronze_storage is required to preserve raw extractor payloads")
        key = self.raw_storage_key(raw_job)
        if self.bronze_storage.exists(key):
            if self.bronze_storage.load(key) != raw_job:
                raise GreenhouseResponseError(f"Bronze hash collision for {key}")
            return None
        return self.bronze_storage.save(key, raw_job)

    @property
    def _board_url(self) -> str:
        return f"{self.api_base_url}/boards/{quote(self.board_token, safe='')}"

    @property
    def _jobs_url(self) -> str:
        return f"{self._board_url}/jobs"

    def _request_json(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Issue a rate-limited request with bounded transient retries."""
        settings = self.config.settings.scraping
        attempts = settings.max_retries + 1
        delay = settings.retry_delay_seconds

        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                response = self._client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise GreenhouseResponseError("Greenhouse response must be a JSON object")
                return dict(payload)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                retryable = True
                error: Exception = exc
            except httpx.HTTPStatusError as exc:
                retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
                error = exc
            except ValueError as exc:
                raise GreenhouseResponseError("Greenhouse returned invalid JSON") from exc

            if not retryable or attempt == attempts:
                raise error

            retry_after = self._retry_after_seconds(getattr(error, "response", None))
            wait = retry_after if retry_after is not None else delay
            log_event(
                "WARNING",
                "Retrying Greenhouse request after attempt {attempt}",
                component="extractor",
                event="greenhouse_request_retry",
                source=self.source_name,
                attempt=attempt,
                wait_seconds=wait,
            )
            self._sleep(wait)
            delay *= 2

        raise RuntimeError("Unreachable Greenhouse retry state")

    def _throttle(self) -> None:
        now = self._monotonic()
        if self._last_request_at is not None:
            wait = self._minimum_request_interval - (now - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
                now = self._monotonic()
        self._last_request_at = now

    @staticmethod
    def _retry_after_seconds(response: httpx.Response | None) -> float | None:
        if response is None:
            return None
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            return None

    def _matches(
        self,
        raw_job: dict[str, Any],
        query: str,
        location: str | None,
        filters: Mapping[str, object],
    ) -> bool:
        query_text = query.strip().casefold()
        searchable = " ".join(
            value
            for value in (
                self._optional_text(raw_job.get("title")),
                self._html_to_text(
                    self._decode_content(self._optional_text(raw_job.get("content")))
                ),
            )
            if value
        ).casefold()
        if query_text and query_text not in searchable:
            return False

        location_name = self._nested_text(raw_job, "location", "name") or ""
        if location and location.casefold() not in location_name.casefold():
            return False

        filter_values = {
            "department": self._named_values(raw_job.get("departments")),
            "office": self._named_values(raw_job.get("offices")),
            "language": [self._optional_text(raw_job.get("language")) or ""],
        }
        return all(
            str(expected).casefold() in " ".join(filter_values[name]).casefold()
            for name, expected in filters.items()
            if expected is not None
        )

    def _workplace_type(self, location: str | None) -> WorkplaceType:
        if not location:
            return WorkplaceType.UNKNOWN
        location_config = self.config.locations.model_dump()
        normalized = location.casefold()
        hybrid_keywords = location_config.get("hybrid_keywords", [])
        remote_keywords = location_config.get("remote_keywords", [])
        if any(str(keyword).casefold() in normalized for keyword in hybrid_keywords):
            return WorkplaceType.HYBRID
        if any(str(keyword).casefold() in normalized for keyword in remote_keywords):
            return WorkplaceType.REMOTE
        return WorkplaceType.ONSITE

    @classmethod
    def _employment_type(cls, raw_job: dict[str, Any]) -> EmploymentType:
        metadata = raw_job.get("metadata")
        values: list[str] = []
        if isinstance(metadata, list):
            for item in metadata:
                if not isinstance(item, dict):
                    continue
                name = cls._optional_text(item.get("name")) or ""
                if "employment" not in name.casefold():
                    continue
                value = item.get("value")
                if isinstance(value, list):
                    values.extend(str(entry) for entry in value)
                elif value is not None:
                    values.append(str(value))
        normalized = " ".join(values).casefold().replace("-", " ")
        mappings = (
            ("full time", EmploymentType.FULL_TIME),
            ("part time", EmploymentType.PART_TIME),
            ("contract", EmploymentType.CONTRACT),
            ("temporary", EmploymentType.TEMPORARY),
            ("intern", EmploymentType.INTERNSHIP),
        )
        return next(
            (kind for phrase, kind in mappings if phrase in normalized),
            EmploymentType.UNKNOWN,
        )

    @staticmethod
    def _decode_content(value: str | None) -> str | None:
        if value is None:
            return None
        decoded = value
        for _ in range(2):
            unescaped = html.unescape(decoded)
            if unescaped == decoded:
                break
            decoded = unescaped
        return decoded

    @staticmethod
    def _html_to_text(value: str | None) -> str | None:
        if value is None:
            return None
        text = BeautifulSoup(value, "lxml").get_text("\n", strip=True)
        return text or None

    @staticmethod
    def _named_values(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        names: list[str] = []
        for item in value:
            if isinstance(item, dict):
                name = GreenhouseExtractor._optional_text(item.get("name"))
                if name:
                    names.append(name)
        return names

    @staticmethod
    def _nested_text(payload: dict[str, Any], key: str, nested_key: str) -> str | None:
        value = payload.get(key)
        if not isinstance(value, dict):
            return None
        return GreenhouseExtractor._optional_text(value.get(nested_key))

    @staticmethod
    def _optional_text(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def _required_text(cls, payload: dict[str, Any], key: str) -> str:
        value = cls._optional_text(payload.get(key))
        if value is None:
            raise GreenhouseResponseError(f"Greenhouse job is missing required field: {key}")
        return value


__all__ = ["GreenhouseExtractor", "GreenhouseResponseError"]
