"""Reusable extractor for Phenom-hosted public career sites."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from extractors.ats_common import ATSResponseError, PublicATSExtractor
from models.job_posting import (
    CompanyInfo,
    DescriptionInfo,
    JobPosting,
    LocationInfo,
    PositionInfo,
    PostingMetadata,
    PostingStatus,
    RawSourceRecord,
    SkillInfo,
    SourceInfo,
    SourceType,
)
from storage.base import BaseStorage
from utils.checkpoint import CheckpointManager
from utils.config import Config
from utils.hashing import stable_json_hash

_DDO_MARKER = "phApp.ddo"
_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


class PhenomExtractor(PublicATSExtractor):
    """Extract jobs from a configured Phenom public career site."""

    source_name = "phenom"

    def __init__(
        self,
        host: str,
        tenant: str,
        site: str,
        *,
        locale: str = "en_us",
        path_prefix: str = "/us/en",
        company_name: str | None = None,
        config: Config | None = None,
        bronze_storage: BaseStorage | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initialize extraction for one Phenom tenant and public site."""
        normalized_host = host.strip().rstrip("/")
        parsed_host = urlparse(normalized_host)
        if parsed_host.scheme != "https" or not parsed_host.netloc:
            raise ValueError("host must be an absolute HTTPS Phenom career-site URL")
        if parsed_host.path not in {"", "/"}:
            raise ValueError("host must contain only the Phenom career-site origin")

        tenant = tenant.strip()
        site = site.strip()
        locale = locale.strip()
        normalized_prefix = "/" + path_prefix.strip().strip("/")
        if not tenant or not site or not locale:
            raise ValueError("tenant, site, and locale must not be empty")
        if normalized_prefix == "/" or any(
            part in {".", ".."} for part in normalized_prefix.split("/")
        ):
            raise ValueError("path_prefix must identify a safe career-site path")

        super().__init__(
            self.source_name,
            site,
            config=config,
            bronze_storage=bronze_storage,
            checkpoint_manager=checkpoint_manager,
            client=client,
            sleep=sleep,
        )
        self.host = normalized_host
        self.tenant = tenant
        self.site = site
        self.locale = locale
        self.path_prefix = normalized_prefix
        self.company_name = company_name.strip() if company_name else tenant
        self._reference_cache: dict[str, dict[str, Any]] = {}

    @property
    def search_url(self) -> str:
        """Return the tenant's public search results page."""
        return f"{self.host}{self.path_prefix}/search-results"

    def search_jobs(self, query: str, location: str | None = None, **filters: object) -> list[str]:
        """Search and paginate Phenom's embedded refine-search payload."""
        unknown = set(filters) - {"facets"}
        if unknown:
            raise ValueError(f"Unsupported Phenom search filters: {', '.join(sorted(unknown))}")
        facets = filters.get("facets", {})
        if not isinstance(facets, Mapping):
            raise ValueError("Phenom facets must be a mapping")

        base_params: dict[str, str | int | float | bool | None] = {
            "keywords": query,
            "location": location or "",
        }
        for key, value in facets.items():
            if not isinstance(key, str) or not isinstance(value, (str, int, float, bool)):
                raise ValueError("Phenom facet names and values must be scalar")
            base_params[key] = value

        self._reference_cache.clear()
        references: list[str] = []
        seen: set[str] = set()
        offset = 0
        total = 0
        while True:
            params = dict(base_params)
            if offset:
                params["from"] = offset
            raw_html = self._request_text("GET", self.search_url, params=params)
            ddo = self._parse_ddo(raw_html)
            search_payload = ddo.get("eagerLoadRefineSearch")
            if not isinstance(search_payload, dict):
                raise ATSResponseError("Phenom page is missing eagerLoadRefineSearch")
            data = search_payload.get("data")
            jobs = data.get("jobs") if isinstance(data, dict) else None
            if not isinstance(jobs, list):
                raise ATSResponseError("Phenom search payload is missing a jobs list")

            total_value = search_payload.get("totalHits")
            total = total_value if isinstance(total_value, int) else total
            added = 0
            for item in jobs:
                if not isinstance(item, dict):
                    continue
                reference = self._job_identifier(item)
                if reference is None or reference in seen:
                    continue
                self._reference_cache[reference] = dict(item)
                references.append(reference)
                seen.add(reference)
                added += 1

            offset += len(jobs)
            if not jobs or not added or (total and offset >= total):
                break
        return references

    def fetch_job(self, job_reference: str) -> dict[str, Any]:
        """Retrieve a full Phenom detail payload and its exact HTML response."""
        reference = str(job_reference).strip()
        if not reference or "/" in reference or "\\" in reference:
            raise ValueError("job_reference must be a Phenom job identifier")
        summary = self._reference_cache.get(reference, {})
        title = self._optional_text(summary.get("title")) or "job"
        slug = _SLUG_PATTERN.sub("-", title.casefold()).strip("-") or "job"
        url = (
            f"{self.host}{self.path_prefix}/job/{quote(reference, safe='')}/{quote(slug, safe='-')}"
        )
        raw_html = self._request_text("GET", url)
        ddo = self._parse_ddo(raw_html)
        detail = ddo.get("jobDetail")
        data = detail.get("data") if isinstance(detail, dict) else None
        job = data.get("job") if isinstance(data, dict) else None
        if not isinstance(job, dict):
            raise ATSResponseError("Phenom page is missing jobDetail.data.job")

        payload = dict(job)
        external_id = self._job_identifier(payload)
        if external_id is None:
            raise ATSResponseError("Phenom detail payload has no stable identifier")
        ref_num = self._optional_text(payload.get("refNum"))
        if ref_num and ref_num.casefold() != self.tenant.casefold():
            raise ATSResponseError("Phenom detail payload belongs to another tenant")
        payload["url"] = url
        payload["raw_html"] = raw_html
        return payload

    def parse_job(self, raw_job: dict[str, Any]) -> JobPosting:
        """Convert a Phenom detail payload into the canonical model."""
        external_id = self._job_identifier(raw_job)
        if external_id is None:
            raise ATSResponseError("Phenom detail payload has no stable identifier")
        title = self._required_text(raw_job, "title")
        company = self._optional_text(raw_job.get("companyName")) or self.company_name
        category = self._optional_text(raw_job.get("category"))
        job_family = self._optional_text(raw_job.get("jobFamily"))
        location = self._optional_text(raw_job.get("location"))
        description_html = self._optional_text(raw_job.get("description"))
        description_text = self._html_to_text(description_html)
        url = self._optional_text(raw_job.get("url"))
        country = self._optional_text(raw_job.get("standardisedCountry") or raw_job.get("country"))
        overview = raw_job.get("ml_job_overview")
        overview_data = overview if isinstance(overview, dict) else {}
        skills = self._string_list(raw_job.get("ml_skills"))
        tags = list(dict.fromkeys(value for value in (category, job_family) if value))

        posting = JobPosting(
            metadata=PostingMetadata(
                job_id="",
                posted_at=self._parse_date(raw_job.get("postedDate")),
                status=PostingStatus.ACTIVE,
                tags=tags,
            ),
            source=SourceInfo.model_validate(
                {
                    "name": self.source_name,
                    "source_type": SourceType.ATS,
                    "url": url,
                    "external_id": external_id,
                }
            ),
            company=CompanyInfo(name=company),
            position=PositionInfo(title=title, family=category or job_family),
            location=LocationInfo(
                raw=location,
                city=self._optional_text(raw_job.get("city")),
                region=self._optional_text(raw_job.get("state")),
                country=country,
                workplace_type=self._workplace_type(
                    " ".join(value for value in (location, description_text) if value)
                ),
                latitude=self._optional_float(raw_job.get("latitude")),
                longitude=self._optional_float(raw_job.get("longitude")),
            ),
            employment_type=self._employment_type(
                raw_job.get("employmentType") or raw_job.get("type")
            ),
            description=DescriptionInfo(
                raw_text=description_text,
                clean_text=description_text,
                html=description_html,
                responsibilities=self._string_list(overview_data.get("responsibilities")),
                qualifications=self._string_list(overview_data.get("education")),
            ),
            skills=SkillInfo(explicit=skills),
            raw=RawSourceRecord.model_validate(
                {"raw_url": url, "raw_payload": raw_job, "raw_text": description_text}
            ),
        )
        return posting.with_derived_metadata()

    def raw_storage_key(self, raw_job: dict[str, Any]) -> str:
        """Version Phenom details by site, identifier, and exact response content."""
        external_id = self._job_identifier(raw_job)
        if external_id is None:
            raise ATSResponseError("Phenom detail payload has no stable identifier")
        safe_id = external_id.replace("/", "_").replace("\\", "_")
        return f"{self.source_name}/{self.site}/{safe_id}/{stable_json_hash(raw_job)}"

    @staticmethod
    def _parse_ddo(raw_html: str) -> dict[str, Any]:
        marker_at = raw_html.find(_DDO_MARKER)
        if marker_at < 0:
            raise ATSResponseError("Phenom page is missing phApp.ddo")
        assignment_at = raw_html.find("=", marker_at + len(_DDO_MARKER))
        if assignment_at < 0:
            raise ATSResponseError("Phenom phApp.ddo assignment is malformed")
        value_at = assignment_at + 1
        while value_at < len(raw_html) and raw_html[value_at].isspace():
            value_at += 1
        try:
            value, _ = json.JSONDecoder().raw_decode(raw_html, value_at)
        except json.JSONDecodeError as exc:
            raise ATSResponseError("Phenom phApp.ddo contains invalid JSON") from exc
        if not isinstance(value, dict):
            raise ATSResponseError("Phenom phApp.ddo must be an object")
        return value

    @classmethod
    def _job_identifier(cls, payload: Mapping[str, Any]) -> str | None:
        return cls._optional_text(
            payload.get("jobId") or payload.get("reqId") or payload.get("jobSeqNo")
        )

    @classmethod
    def _string_list(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [text for item in value if (text := cls._optional_text(item))]

    @staticmethod
    def _optional_float(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


__all__ = ["PhenomExtractor"]
