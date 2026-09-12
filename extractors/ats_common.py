"""Shared HTTP and parsing support for public ATS extractors."""

from __future__ import annotations

import html
import time
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Self

import httpx
from bs4 import BeautifulSoup

from extractors.base_extractor import BaseExtractor
from models.job_posting import EmploymentType, PayPeriod, WorkplaceType
from storage.base import BaseStorage
from utils.checkpoint import CheckpointManager
from utils.config import Config
from utils.hashing import stable_json_hash
from utils.logger import log_event


class ATSResponseError(RuntimeError):
    """Raised when a public ATS returns an unusable response."""


class PublicATSExtractor(BaseExtractor):
    """Base class for public career feeds with shared operational behavior."""

    def __init__(
        self,
        source_name: str,
        storage_scope: str,
        *,
        config: Config | None = None,
        bronze_storage: BaseStorage | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize shared storage, throttling, and HTTP state."""
        super().__init__(
            source_name,
            config=config,
            bronze_storage=bronze_storage,
            checkpoint_manager=checkpoint_manager,
        )
        source = self.source_definition
        if source is None:
            raise ValueError(f"Missing {source_name} source configuration")
        if not source.enabled:
            raise ValueError(f"The {source_name} source is disabled")
        self.storage_scope = storage_scope
        self._sleep = sleep
        self._monotonic = monotonic
        self._minimum_request_interval = 60.0 / source.rate_limit_per_minute
        self._last_request_at: float | None = None
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=self.config.settings.scraping.request_timeout_seconds,
            headers={
                "Accept": "application/json",
                "User-Agent": self.config.settings.scraping.user_agent,
            },
            follow_redirects=True,
        )

    def close(self) -> None:
        """Close the internally owned HTTP client."""
        if self._owns_client:
            self._client.close()

    @property
    def checkpoint_name(self) -> str:
        """Return a checkpoint name scoped to this ATS site."""
        return f"{self.source_name}-{self.storage_scope}-extractor"

    def __enter__(self) -> Self:
        """Return the extractor for context-managed use."""
        return self

    def __exit__(self, *_args: object) -> None:
        """Release the internally owned HTTP client."""
        self.close()

    def raw_storage_key(self, raw_job: dict[str, Any]) -> str:
        """Version raw snapshots by source scope, identity, and content."""
        external_id = self._raw_external_id(raw_job)
        if external_id is None:
            raise ATSResponseError(f"{self.source_name} payload has no external identifier")
        return (
            f"{self.source_name}/{self.storage_scope}/{external_id}/"
            f"{stable_json_hash(raw_job)}"
        )

    def save_raw(self, raw_job: dict[str, Any]) -> Path | None:
        """Persist a new raw version while keeping identical snapshots idempotent."""
        if self.bronze_storage is None:
            raise RuntimeError("bronze_storage is required to preserve raw extractor payloads")
        key = self.raw_storage_key(raw_job)
        if self.bronze_storage.exists(key):
            if self.bronze_storage.load(key) != raw_job:
                raise ATSResponseError(f"Bronze hash collision for {key}")
            return None
        return self.bronze_storage.save(key, raw_job)

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int | float | bool | None] | None = None,
        json_payload: Mapping[str, object] | None = None,
    ) -> object:
        """Issue a rate-limited JSON request with bounded transient retries."""
        response = self._request_response(
            method,
            url,
            params=params,
            json_payload=json_payload,
        )
        try:
            return response.json()
        except ValueError as exc:
            raise ATSResponseError(f"{self.source_name} returned invalid JSON") from exc

    def _request_text(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> str:
        """Issue a rate-limited text request with bounded transient retries."""
        return self._request_response(method, url, params=params).text

    def _request_response(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int | float | bool | None] | None = None,
        json_payload: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        """Return a successful response after applying the shared retry policy."""
        settings = self.config.settings.scraping
        attempts = settings.max_retries + 1
        delay = settings.retry_delay_seconds
        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                response = self._client.request(method, url, params=params, json=json_payload)
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                retryable = True
                error: Exception = exc
            except httpx.HTTPStatusError as exc:
                retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
                error = exc
            if not retryable or attempt == attempts:
                raise error
            candidate_response = getattr(error, "response", None)
            error_response = (
                candidate_response if isinstance(candidate_response, httpx.Response) else None
            )
            retry_after = self._retry_after_seconds(error_response)
            wait = retry_after if retry_after is not None else delay
            log_event(
                "WARNING",
                "Retrying {source} request after attempt {attempt}",
                component="extractor",
                event=f"{self.source_name}_request_retry",
                source=self.source_name,
                attempt=attempt,
                wait_seconds=wait,
            )
            self._sleep(wait)
            delay *= 2
        raise RuntimeError("Unreachable ATS retry state")

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
        if response is None or "Retry-After" not in response.headers:
            return None
        try:
            return max(0.0, float(response.headers["Retry-After"]))
        except ValueError:
            return None

    @staticmethod
    def _optional_text(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def _required_text(cls, payload: Mapping[str, Any], key: str) -> str:
        value = cls._optional_text(payload.get(key))
        if value is None:
            raise ATSResponseError(f"ATS payload is missing required field: {key}")
        return value

    @staticmethod
    def _html_to_text(value: str | None) -> str | None:
        if value is None:
            return None
        decoded = html.unescape(value)
        text = BeautifulSoup(decoded, "lxml").get_text("\n", strip=True)
        return text or None

    @staticmethod
    def _parse_date(value: object) -> date | None:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, UTC).date()
        text = PublicATSExtractor._optional_text(value)
        if text is None:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None

    @staticmethod
    def _workplace_type(value: object) -> WorkplaceType:
        normalized = (
            (PublicATSExtractor._optional_text(value) or "")
            .casefold()
            .replace("-", "")
            .replace(" ", "")
        )
        if "hybrid" in normalized:
            return WorkplaceType.HYBRID
        if "remote" in normalized:
            return WorkplaceType.REMOTE
        if "onsite" in normalized:
            return WorkplaceType.ONSITE
        return WorkplaceType.UNKNOWN

    @staticmethod
    def _employment_type(value: object) -> EmploymentType:
        normalized = (
            (PublicATSExtractor._optional_text(value) or "")
            .casefold()
            .replace("-", "")
            .replace(" ", "")
        )
        mappings = (
            ("fulltime", EmploymentType.FULL_TIME),
            ("parttime", EmploymentType.PART_TIME),
            ("contract", EmploymentType.CONTRACT),
            ("temporary", EmploymentType.TEMPORARY),
            ("intern", EmploymentType.INTERNSHIP),
        )
        return next(
            (kind for phrase, kind in mappings if phrase in normalized),
            EmploymentType.UNKNOWN,
        )

    @staticmethod
    def _pay_period(value: object) -> PayPeriod:
        normalized = (PublicATSExtractor._optional_text(value) or "").casefold()
        if "hour" in normalized:
            return PayPeriod.HOURLY
        if "month" in normalized:
            return PayPeriod.MONTHLY
        if "year" in normalized or "annual" in normalized:
            return PayPeriod.YEARLY
        return PayPeriod.UNKNOWN


__all__ = ["ATSResponseError", "PublicATSExtractor"]
