"""Ashby public Job Postings API extractor."""

from __future__ import annotations

import copy
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
    SalaryInfo,
    SourceInfo,
    SourceType,
)
from storage.base import BaseStorage
from utils.checkpoint import CheckpointManager
from utils.config import Config
from utils.hashing import stable_json_hash


class AshbyExtractor(PublicATSExtractor):
    """Extract currently published jobs from one Ashby job board."""

    source_name = "ashby"

    def __init__(
        self,
        job_board_name: str,
        *,
        company_name: str | None = None,
        include_unlisted: bool = False,
        config: Config | None = None,
        bronze_storage: BaseStorage | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initialize extraction for an Ashby job-board name."""
        board = job_board_name.strip()
        if not board:
            raise ValueError("job_board_name must not be empty")
        super().__init__(
            self.source_name,
            board,
            config=config,
            bronze_storage=bronze_storage,
            checkpoint_manager=checkpoint_manager,
            client=client,
            sleep=sleep,
        )
        source = self.source_definition
        assert source is not None
        self.job_board_name = board
        self.company_name = company_name.strip() if company_name else board
        self.include_unlisted = include_unlisted
        self.api_base_url = (source.api_base_url or source.base_url).rstrip("/")
        self._job_cache: dict[str, dict[str, Any]] = {}

    @property
    def _board_url(self) -> str:
        return f"{self.api_base_url}/{quote(self.job_board_name, safe='')}"

    def search_jobs(
        self, query: str, location: str | None = None, **filters: object
    ) -> list[str]:
        """Return Ashby jobs matching canonical search criteria."""
        supported = {"department", "team", "employment_type", "workplace_type"}
        unknown = set(filters) - supported
        if unknown:
            raise ValueError(f"Unsupported Ashby search filters: {', '.join(sorted(unknown))}")
        payload = self._request_json(
            "GET", self._board_url, params={"includeCompensation": "true"}
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ATSResponseError("Ashby response is missing a jobs list")
        self._job_cache.clear()
        references: list[str] = []
        for item in payload["jobs"]:
            if not isinstance(item, dict):
                continue
            raw_job = dict(item)
            if not self.include_unlisted and raw_job.get("isListed") is False:
                continue
            reference = self._job_id(raw_job)
            if reference is None or not self._matches(raw_job, query, location, filters):
                continue
            self._job_cache[reference] = raw_job
            references.append(reference)
        return references

    def fetch_job(self, job_reference: str) -> dict[str, Any]:
        """Return a cached job, refreshing the board once when necessary."""
        cached = self._job_cache.get(str(job_reference))
        if cached is None:
            self.search_jobs("")
            cached = self._job_cache.get(str(job_reference))
        if cached is None:
            raise KeyError(f"Ashby job not found: {job_reference}")
        return copy.deepcopy(cached)

    def parse_job(self, raw_job: dict[str, Any]) -> JobPosting:
        """Convert an Ashby posting into the canonical model."""
        external_id = self._job_id(raw_job)
        if external_id is None:
            raise ATSResponseError("Ashby job has no stable identifier")
        title = self._required_text(raw_job, "title")
        url = self._optional_text(raw_job.get("jobUrl"))
        department = self._optional_text(raw_job.get("department"))
        team = self._optional_text(raw_job.get("team"))
        location = self._optional_text(raw_job.get("location"))
        address = self._postal_address(raw_job)
        description_html = self._optional_text(raw_job.get("descriptionHtml"))
        description_text = self._optional_text(raw_job.get("descriptionPlain"))
        posting = JobPosting(
            metadata=PostingMetadata(
                job_id="",
                posted_at=self._parse_date(raw_job.get("publishedAt")),
                status=PostingStatus.ACTIVE,
                tags=[value for value in (department, team) if value],
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
                city=self._optional_text(address.get("addressLocality")),
                region=self._optional_text(address.get("addressRegion")),
                country=self._optional_text(address.get("addressCountry")),
                workplace_type=self._workplace_type(raw_job.get("workplaceType")),
            ),
            salary=self._salary(raw_job),
            employment_type=self._employment_type(raw_job.get("employmentType")),
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

    def raw_storage_key(self, raw_job: dict[str, Any]) -> str:
        """Version Ashby snapshots using a stable posting identifier."""
        external_id = self._job_id(raw_job)
        if external_id is None:
            raise ATSResponseError("Ashby job has no stable identifier")
        return f"{self.source_name}/{self.storage_scope}/{external_id}/{stable_json_hash(raw_job)}"

    def _matches(
        self,
        raw_job: dict[str, Any],
        query: str,
        location: str | None,
        filters: Mapping[str, object],
    ) -> bool:
        searchable = " ".join(
            str(raw_job.get(key, "")) for key in ("title", "descriptionPlain")
        ).casefold()
        if query.strip() and query.casefold() not in searchable:
            return False
        location_text = self._optional_text(raw_job.get("location")) or ""
        if location and location.casefold() not in location_text.casefold():
            return False
        fields = {
            "department": raw_job.get("department"),
            "team": raw_job.get("team"),
            "employment_type": raw_job.get("employmentType"),
            "workplace_type": raw_job.get("workplaceType"),
        }
        return all(
            str(expected).casefold() in str(fields[name] or "").casefold()
            for name, expected in filters.items()
            if expected is not None
        )

    @staticmethod
    def _job_id(raw_job: Mapping[str, Any]) -> str | None:
        explicit = PublicATSExtractor._optional_text(raw_job.get("id"))
        if explicit:
            return explicit
        job_url = PublicATSExtractor._optional_text(raw_job.get("jobUrl"))
        if not job_url:
            return None
        return urlparse(job_url).path.rstrip("/").split("/")[-1] or None

    @staticmethod
    def _postal_address(raw_job: Mapping[str, Any]) -> Mapping[str, Any]:
        address = raw_job.get("address")
        if not isinstance(address, dict):
            return {}
        postal = address.get("postalAddress")
        return postal if isinstance(postal, dict) else {}

    def _salary(self, raw_job: Mapping[str, Any]) -> SalaryInfo:
        compensation = raw_job.get("compensation")
        data = compensation if isinstance(compensation, dict) else {}
        components = data.get("summaryComponents")
        if not isinstance(components, list):
            components = []
        salary = next(
            (
                item
                for item in components
                if isinstance(item, dict)
                and str(item.get("compensationType", "")).casefold() == "salary"
            ),
            {},
        )
        return SalaryInfo(
            min_amount=self._number(salary.get("minValue")),
            max_amount=self._number(salary.get("maxValue")),
            currency=self._optional_text(salary.get("currencyCode")) or "USD",
            pay_period=self._pay_period(salary.get("interval")),
            raw_text=self._optional_text(data.get("scrapeableCompensationSalarySummary")),
        )

    @staticmethod
    def _number(value: object) -> float | None:
        return float(value) if isinstance(value, (int, float)) else None


__all__ = ["AshbyExtractor"]
