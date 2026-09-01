"""Lever Postings API extractor."""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

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
    SalaryInfo,
    SourceInfo,
    SourceType,
)
from storage.base import BaseStorage
from utils.checkpoint import CheckpointManager
from utils.config import Config


class LeverExtractor(PublicATSExtractor):
    """Extract published jobs from one Lever site."""

    source_name = "lever"

    def __init__(
        self,
        site: str,
        *,
        company_name: str | None = None,
        config: Config | None = None,
        bronze_storage: BaseStorage | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initialize extraction for a Lever site name."""
        site = site.strip()
        if not site:
            raise ValueError("site must not be empty")
        super().__init__(
            self.source_name,
            site,
            config=config,
            bronze_storage=bronze_storage,
            checkpoint_manager=checkpoint_manager,
            client=client,
            sleep=sleep,
        )
        source = self.source_definition
        assert source is not None
        self.site = site
        self.company_name = company_name.strip() if company_name else site
        self.api_base_url = (source.api_base_url or source.base_url).rstrip("/")
        self._job_cache: dict[str, dict[str, Any]] = {}

    @property
    def _site_url(self) -> str:
        return f"{self.api_base_url}/{quote(self.site, safe='')}"

    def search_jobs(
        self, query: str, location: str | None = None, **filters: object
    ) -> list[str]:
        """Return postings matching text and category filters."""
        supported = {"department", "team", "commitment", "workplace_type"}
        unknown = set(filters) - supported
        if unknown:
            raise ValueError(f"Unsupported Lever search filters: {', '.join(sorted(unknown))}")

        payload = self._request_json("GET", self._site_url, params={"mode": "json"})
        if not isinstance(payload, list):
            raise ATSResponseError("Lever postings response must be a list")
        self._job_cache.clear()
        references: list[str] = []
        for item in payload:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            raw_job = dict(item)
            if not self._matches(raw_job, query, location, filters):
                continue
            reference = str(raw_job["id"])
            self._job_cache[reference] = raw_job
            references.append(reference)
        return references

    def fetch_job(self, job_reference: str) -> dict[str, Any]:
        """Return a cached posting or retrieve one by ID."""
        cached = self._job_cache.get(str(job_reference))
        if cached is not None:
            return copy.deepcopy(cached)
        payload = self._request_json(
            "GET", f"{self._site_url}/{quote(str(job_reference), safe='')}", params={"mode": "json"}
        )
        if not isinstance(payload, dict):
            raise ATSResponseError("Lever detail response must be an object")
        return dict(payload)

    def parse_job(self, raw_job: dict[str, Any]) -> JobPosting:
        """Convert a Lever posting into the canonical model."""
        external_id = self._required_text(raw_job, "id")
        title = self._required_text(raw_job, "text")
        categories = raw_job.get("categories")
        category_map = categories if isinstance(categories, dict) else {}
        location = self._optional_text(category_map.get("location"))
        description_html = self._optional_text(raw_job.get("description"))
        description_text = self._optional_text(raw_job.get("descriptionPlain"))
        salary = self._salary(raw_job)
        url = self._optional_text(raw_job.get("hostedUrl"))
        department = self._optional_text(category_map.get("department"))
        team = self._optional_text(category_map.get("team"))
        tags = [value for value in (department, team) if value]

        posting = JobPosting(
            metadata=PostingMetadata(
                job_id="",
                posted_at=self._parse_date(raw_job.get("createdAt")),
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
            company=CompanyInfo(name=self.company_name),
            position=PositionInfo(title=title, family=department or team),
            location=LocationInfo(
                raw=location,
                country=self._optional_text(raw_job.get("country")),
                workplace_type=self._workplace_type(raw_job.get("workplaceType")),
            ),
            salary=salary,
            employment_type=self._employment_type(category_map.get("commitment")),
            description=DescriptionInfo(
                raw_text=description_text,
                clean_text=description_text or self._html_to_text(description_html),
                html=description_html,
            ),
            raw=RawSourceRecord.model_validate(
                {"raw_url": url, "raw_payload": raw_job, "raw_text": description_text}
            ),
        )
        return posting.with_derived_metadata()

    def _matches(
        self,
        raw_job: dict[str, Any],
        query: str,
        location: str | None,
        filters: Mapping[str, object],
    ) -> bool:
        categories = raw_job.get("categories")
        values = categories if isinstance(categories, dict) else {}
        searchable = " ".join(
            str(raw_job.get(key, "")) for key in ("text", "descriptionPlain")
        ).casefold()
        if query.strip() and query.casefold() not in searchable:
            return False
        location_text = self._optional_text(values.get("location")) or ""
        if location and location.casefold() not in location_text.casefold():
            return False
        available = {
            "department": values.get("department"),
            "team": values.get("team"),
            "commitment": values.get("commitment"),
            "workplace_type": raw_job.get("workplaceType"),
        }
        return all(
            str(expected).casefold() in str(available[name] or "").casefold()
            for name, expected in filters.items()
            if expected is not None
        )

    def _salary(self, raw_job: dict[str, Any]) -> SalaryInfo:
        value = raw_job.get("salaryRange")
        salary = value if isinstance(value, dict) else {}
        return SalaryInfo(
            min_amount=self._number(salary.get("min")),
            max_amount=self._number(salary.get("max")),
            currency=self._optional_text(salary.get("currency")) or "USD",
            pay_period=self._pay_period(salary.get("interval")),
            raw_text=self._optional_text(raw_job.get("salaryDescriptionPlain")),
        )

    @staticmethod
    def _number(value: object) -> float | None:
        return float(value) if isinstance(value, (int, float)) else None


__all__ = ["LeverExtractor"]
