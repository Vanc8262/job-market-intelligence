"""NextEra Energy public career-site extractor."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

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

_SPACE_PATTERN = re.compile(r"\s+")


class NextEraExtractor(PublicATSExtractor):
    """Extract server-rendered jobs from NextEra Energy's public career site."""

    source_name = "nextera"
    page_size = 25

    def __init__(
        self,
        host: str,
        site: str,
        *,
        company_name: str | None = None,
        config: Config | None = None,
        bronze_storage: BaseStorage | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initialize extraction for one NextEra public career site."""
        normalized_host = host.strip().rstrip("/")
        parsed_host = urlparse(normalized_host)
        if parsed_host.scheme != "https" or not parsed_host.netloc:
            raise ValueError("host must be an absolute HTTPS NextEra career-site URL")
        if parsed_host.path not in {"", "/"}:
            raise ValueError("host must contain only the NextEra career-site origin")
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
        self.host = normalized_host
        self.site = site
        self.company_name = company_name.strip() if company_name else "NextEra Energy"

    @property
    def search_url(self) -> str:
        """Return the public search endpoint."""
        return f"{self.host}/search/"

    def search_jobs(self, query: str, location: str | None = None, **filters: object) -> list[str]:
        """Search and paginate public NextEra job links."""
        unknown = set(filters) - {"distance", "searchby"}
        if unknown:
            raise ValueError(f"Unsupported NextEra search filters: {', '.join(sorted(unknown))}")

        distance_value = filters.get("distance", 10)
        if not isinstance(distance_value, (str, int)):
            raise ValueError("NextEra distance must be an integer")
        try:
            distance = int(distance_value)
        except ValueError as exc:
            raise ValueError("NextEra distance must be an integer") from exc
        if distance < 1:
            raise ValueError("NextEra distance must be greater than zero")

        params: dict[str, str | int] = {
            "q": query,
            "locationsearch": location or "",
            "searchby": str(filters.get("searchby") or "location"),
            "d": distance,
        }
        references: list[str] = []
        seen: set[str] = set()
        start_row = 0
        while True:
            page_params = dict(params)
            if start_row:
                page_params["startrow"] = start_row
            html = self._request_text("GET", self.search_url, params=page_params)
            page_references = self._search_references(html)
            new_references = [reference for reference in page_references if reference not in seen]
            references.extend(new_references)
            seen.update(new_references)
            if len(page_references) < self.page_size or not new_references:
                break
            start_row += self.page_size
        return references

    def fetch_job(self, job_reference: str) -> dict[str, Any]:
        """Retrieve and structure one public NextEra detail response."""
        url = self._job_url(job_reference)
        raw_html = self._request_text("GET", url)
        soup = BeautifulSoup(raw_html, "lxml")
        title = self._selected_text(soup, "h1")
        location = self._without_label(self._selected_text(soup, ".jobLocation"), "Location(s):")
        company = self._without_label(self._selected_text(soup, ".jobCompany"), "Company:")
        posted_on = self._without_label(self._selected_text(soup, ".jobDate"), "Date:")
        description_node = soup.select_one(".jobdescription")
        if not isinstance(description_node, Tag):
            raise ATSResponseError("NextEra detail page is missing a job description")
        description_html = description_node.decode_contents().strip()
        description_text = self._clean_text(description_node.get_text(" ", strip=True))
        requisition_id = self._labeled_value(description_text, "Requisition ID")
        page_id = urlparse(url).path.rstrip("/").split("/")[-1]
        external_id = requisition_id or page_id
        if not external_id:
            raise ATSResponseError("NextEra detail page has no stable identifier")
        return {
            "id": external_id,
            "page_id": page_id,
            "url": url,
            "title": title,
            "company": company or self.company_name,
            "location": location,
            "posted_on": posted_on,
            "requisition_id": requisition_id,
            "employee_group": self._labeled_value(description_text, "Employee Group"),
            "employee_type": self._labeled_value(description_text, "Employee Type"),
            "job_category": self._labeled_value(description_text, "Job Category"),
            "organization": self._labeled_value(description_text, "Organization"),
            "description_html": description_html,
            "description_text": description_text,
            "raw_html": raw_html,
        }

    def parse_job(self, raw_job: dict[str, Any]) -> JobPosting:
        """Convert a structured NextEra detail response into the canonical model."""
        external_id = self._required_text(raw_job, "id")
        title = self._required_text(raw_job, "title")
        url = self._optional_text(raw_job.get("url"))
        location = self._optional_text(raw_job.get("location"))
        description_html = self._optional_text(raw_job.get("description_html"))
        description_text = self._optional_text(raw_job.get("description_text"))
        company = self._optional_text(raw_job.get("company")) or self.company_name
        category = self._optional_text(raw_job.get("job_category"))
        organization = self._optional_text(raw_job.get("organization"))
        employee_group = self._optional_text(raw_job.get("employee_group"))
        tags = [value for value in (category, organization, employee_group) if value]
        workplace_text = " ".join(value for value in (location, description_text) if value)

        posting = JobPosting(
            metadata=PostingMetadata(
                job_id="",
                posted_at=self._next_era_date(raw_job.get("posted_on")),
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
            position=PositionInfo(title=title, family=category),
            location=LocationInfo(
                raw=location,
                country="United States" if location and ", US" in location else None,
                workplace_type=self._workplace_type(workplace_text),
            ),
            employment_type=self._employment_type(raw_job.get("employee_type")),
            description=DescriptionInfo(
                raw_text=description_text,
                clean_text=description_text,
                html=description_html,
            ),
            raw=RawSourceRecord.model_validate(
                {"raw_url": url, "raw_payload": raw_job, "raw_text": description_text}
            ),
        )
        return posting.with_derived_metadata()

    def _job_url(self, reference: str) -> str:
        value = str(reference).strip()
        if not value:
            raise ValueError("job_reference must not be empty")
        url = urljoin(f"{self.host}/", value)
        parsed = urlparse(url)
        configured_host = urlparse(self.host).netloc.casefold()
        if parsed.scheme != "https" or parsed.netloc.casefold() != configured_host:
            raise ValueError("job_reference must remain on the configured NextEra host")
        if "/job/" not in parsed.path:
            raise ValueError("job_reference must identify a NextEra job detail page")
        return url

    @staticmethod
    def _search_references(raw_html: str) -> list[str]:
        soup = BeautifulSoup(raw_html, "lxml")
        references: list[str] = []
        for anchor in soup.select("a.jobTitle-link[href]"):
            reference = str(anchor.get("href") or "").strip()
            if reference and reference not in references:
                references.append(reference)
        return references

    @classmethod
    def _selected_text(cls, soup: BeautifulSoup, selector: str) -> str:
        node = soup.select_one(selector)
        if not isinstance(node, Tag):
            raise ATSResponseError(f"NextEra detail page is missing {selector}")
        text = cls._clean_text(node.get_text(" ", strip=True))
        if not text:
            raise ATSResponseError(f"NextEra detail page has an empty {selector}")
        return text

    @classmethod
    def _without_label(cls, value: str, label: str) -> str | None:
        cleaned = re.sub(rf"^{re.escape(label)}\s*", "", value, flags=re.IGNORECASE)
        return cls._optional_text(cleaned)

    @classmethod
    def _labeled_value(cls, value: str, label: str) -> str | None:
        if label.casefold() == "requisition id":
            match = re.search(r"\bRequisition ID:\s*([A-Za-z0-9-]+)", value, re.IGNORECASE)
            return cls._optional_text(match.group(1)) if match else None
        labels = "Employee Group|Employee Type|Job Category|Organization|Relocation Provided"
        pattern = rf"\b{re.escape(label)}:\s*(.*?)(?=\s+(?:{labels}):|$)"
        match = re.search(pattern, value, flags=re.IGNORECASE)
        return cls._optional_text(match.group(1)) if match else None

    @staticmethod
    def _clean_text(value: str) -> str:
        return _SPACE_PATTERN.sub(" ", value.replace("\ufffd", " ")).strip()

    @staticmethod
    def _next_era_date(value: object) -> date | None:
        text = PublicATSExtractor._optional_text(value)
        if text is None:
            return None
        try:
            return datetime.strptime(text, "%b %d, %Y").date()
        except ValueError:
            return PublicATSExtractor._parse_date(text)


__all__ = ["NextEraExtractor"]
