"""Workday public career-site CXS extractor."""

from __future__ import annotations

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
    SourceInfo,
    SourceType,
)
from storage.base import BaseStorage
from utils.checkpoint import CheckpointManager
from utils.config import Config
from utils.hashing import stable_json_hash


class WorkdayExtractor(PublicATSExtractor):
    """Extract public jobs from a specific Workday tenant career site."""

    source_name = "workday"

    def __init__(
        self,
        host: str,
        tenant: str,
        site: str,
        *,
        company_name: str | None = None,
        page_size: int = 20,
        config: Config | None = None,
        bronze_storage: BaseStorage | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initialize extraction for a Workday host, tenant, and career site."""
        normalized_host = host.strip().rstrip("/")
        parsed_host = urlparse(normalized_host)
        if parsed_host.scheme != "https" or not parsed_host.netloc:
            raise ValueError("host must be an absolute HTTPS Workday career-site URL")
        if parsed_host.path not in {"", "/"}:
            raise ValueError("host must contain only the Workday career-site origin")
        tenant = tenant.strip()
        site = site.strip()
        if not tenant or not site:
            raise ValueError("tenant and site must not be empty")
        if not 1 <= page_size <= 20:
            raise ValueError("page_size must be between 1 and 20")
        scope = f"{tenant}/{site}"
        super().__init__(
            self.source_name,
            scope,
            config=config,
            bronze_storage=bronze_storage,
            checkpoint_manager=checkpoint_manager,
            client=client,
            sleep=sleep,
        )
        self.host = normalized_host
        self.tenant = tenant
        self.site = site
        self.company_name = company_name.strip() if company_name else tenant
        self.page_size = page_size
        self._reference_cache: dict[str, dict[str, Any]] = {}

    @property
    def _cxs_url(self) -> str:
        return (
            f"{self.host}/wday/cxs/{quote(self.tenant, safe='')}/"
            f"{quote(self.site, safe='')}"
        )

    def search_jobs(
        self, query: str, location: str | None = None, **filters: object
    ) -> list[str]:
        """Search and paginate a Workday career site.

        The optional ``facets`` filter accepts the site's native applied-facet
        mapping. Other filtering remains source-agnostic and local.
        """
        unknown = set(filters) - {"facets"}
        if unknown:
            raise ValueError(f"Unsupported Workday search filters: {', '.join(sorted(unknown))}")
        facets = filters.get("facets", {})
        if not isinstance(facets, dict):
            raise ValueError("Workday facets must be a mapping")

        self._reference_cache.clear()
        references: list[str] = []
        offset = 0
        while True:
            payload = self._request_json(
                "POST",
                f"{self._cxs_url}/jobs",
                json_payload={
                    "appliedFacets": facets,
                    "limit": self.page_size,
                    "offset": offset,
                    "searchText": query,
                },
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("jobPostings"), list):
                raise ATSResponseError("Workday response is missing a jobPostings list")
            postings = payload["jobPostings"]
            for item in postings:
                if not isinstance(item, dict):
                    continue
                reference = self._optional_text(item.get("externalPath"))
                if reference is None:
                    continue
                location_text = self._optional_text(item.get("locationsText")) or ""
                if location and location.casefold() not in location_text.casefold():
                    continue
                self._reference_cache[reference] = dict(item)
                references.append(reference)
            offset += len(postings)
            total = payload.get("total")
            if not postings or len(postings) < self.page_size:
                break
            if isinstance(total, int) and offset >= total:
                break
        return references

    def fetch_job(self, job_reference: str) -> dict[str, Any]:
        """Retrieve a Workday posting detail by its external path."""
        reference = str(job_reference).strip()
        if not reference:
            raise ValueError("job_reference must not be empty")
        path = reference if reference.startswith("/") else f"/{reference}"
        payload = self._request_json("GET", f"{self._cxs_url}{quote(path, safe='/')}")
        if not isinstance(payload, dict):
            raise ATSResponseError("Workday detail response must be an object")
        return dict(payload)

    def parse_job(self, raw_job: dict[str, Any]) -> JobPosting:
        """Convert a Workday CXS detail payload into the canonical model."""
        info_value = raw_job.get("jobPostingInfo")
        info = info_value if isinstance(info_value, dict) else raw_job
        title = self._required_text(info, "title")
        external_id = (
            self._optional_text(info.get("jobReqId"))
            or self._optional_text(info.get("jobId"))
            or self._optional_text(raw_job.get("externalPath"))
        )
        if external_id is None:
            raise ATSResponseError("Workday job has no stable identifier")
        external_path = self._optional_text(raw_job.get("externalPath"))
        url = self._optional_text(info.get("externalUrl"))
        if url is None and external_path:
            url = f"{self.host}{external_path}"
        location = self._optional_text(info.get("location"))
        description_html = self._optional_text(info.get("jobDescription"))
        description_text = self._html_to_text(description_html)
        department = (
            self._optional_text(info.get("jobFamilyGroup"))
            or self._optional_text(info.get("jobFamily"))
            or self._optional_text(info.get("jobProfile"))
        )
        workplace = info.get("remoteType") or info.get("workplaceType") or location
        employment = info.get("timeType") or info.get("workerType")
        posting = JobPosting(
            metadata=PostingMetadata(
                job_id="",
                posted_at=self._parse_date(info.get("startDate") or info.get("postedOn")),
                status=PostingStatus.ACTIVE,
                tags=[department] if department else [],
            ),
            source=SourceInfo.model_validate(
                {
                    "name": self.source_name,
                    "source_type": SourceType.ATS,
                    "url": url,
                    "external_id": external_id,
                }
            ),
            company=CompanyInfo(name=self.company_name),
            position=PositionInfo(title=title, family=department),
            location=LocationInfo(
                raw=location,
                country=self._optional_text(info.get("locationCountry")),
                workplace_type=self._workplace_type(workplace),
            ),
            employment_type=self._employment_type(employment),
            description=DescriptionInfo(
                raw_text=description_text,
                clean_text=description_text,
                html=description_html,
            ),
            raw=RawSourceRecord.model_validate(
                {"raw_url": url, "raw_payload": raw_job, "raw_text": description_html}
            ),
        )
        return posting.with_derived_metadata()

    def raw_storage_key(self, raw_job: dict[str, Any]) -> str:
        """Version Workday details by requisition/path identity and content."""
        info_value = raw_job.get("jobPostingInfo")
        info: Mapping[str, Any] = info_value if isinstance(info_value, dict) else raw_job
        external_id = (
            self._optional_text(info.get("jobReqId"))
            or self._optional_text(info.get("jobId"))
            or self._optional_text(raw_job.get("externalPath"))
        )
        if external_id is None:
            raise ATSResponseError("Workday job has no stable identifier")
        safe_id = external_id.strip("/").replace("/", "_")
        return f"{self.source_name}/{self.storage_scope}/{safe_id}/{stable_json_hash(raw_job)}"


__all__ = ["WorkdayExtractor"]
