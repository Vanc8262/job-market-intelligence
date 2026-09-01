"""Contract tests for reusable Phenom career-site extraction."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from extractors import PhenomExtractor
from extractors.ats_common import ATSResponseError
from models.job_posting import EmploymentType, WorkplaceType
from storage import BronzeStorage, SilverStorage
from utils.checkpoint import CheckpointManager
from utils.config import Config, ExtractorTargetsConfig


def phenom_page(ddo: dict[str, object]) -> str:
    """Embed a DDO payload in representative Phenom page markup."""
    return (
        "<html><script>window.phApp = window.phApp || {}; "
        f"phApp.ddo = {json.dumps(ddo)}; phApp.bootstrap = true;</script></html>"
    )


def search_ddo(job_ids: list[str], total: int) -> dict[str, object]:
    """Return a representative eager-loaded Phenom search payload."""
    return {
        "siteConfig": {"data": {"siteSettings": {"size": "2"}}},
        "eagerLoadRefineSearch": {
            "status": 200,
            "hits": len(job_ids),
            "totalHits": total,
            "data": {
                "jobs": [
                    {
                        "jobId": job_id,
                        "reqId": job_id,
                        "title": f"Data Engineer {job_id}",
                        "location": "Boston, MA",
                    }
                    for job_id in job_ids
                ]
            },
        },
    }


def detail_job(job_id: str = "R123") -> dict[str, object]:
    """Return a representative Phenom detail record."""
    return {
        "jobId": job_id,
        "reqId": job_id,
        "refNum": "ACMEUS",
        "title": "Senior Data Engineer",
        "companyName": "Acme, Inc.",
        "category": "Technology",
        "jobFamily": "Data Engineering",
        "location": "Boston, MA",
        "city": "Boston",
        "state": "Massachusetts",
        "standardisedCountry": "United States",
        "latitude": "42.3601",
        "longitude": "-71.0589",
        "employmentType": "Full time",
        "postedDate": "2026-08-20T00:00:00.000+0000",
        "description": (
            "<p>This hybrid role builds pipelines with Python, SQL, and AWS.</p>"
        ),
        "ml_skills": ["python", "sql", "aws"],
        "ml_job_overview": {
            "responsibilities": ["Build reliable data pipelines."],
            "education": ["Bachelor's degree or equivalent experience."],
        },
    }


def detail_ddo(job_id: str = "R123") -> dict[str, object]:
    """Return a representative Phenom job-detail DDO."""
    return {"jobDetail": {"status": 200, "data": {"job": detail_job(job_id)}}}


def make_extractor(
    client: httpx.Client, *, bronze_storage: BronzeStorage | None = None
) -> PhenomExtractor:
    """Build an isolated generic Phenom extractor."""
    return PhenomExtractor(
        "https://careers.acme.example",
        "ACMEUS",
        "acme",
        company_name="Acme, Inc.",
        bronze_storage=bronze_storage,
        client=client,
        sleep=lambda _seconds: None,
    )


def test_phenom_paginates_embedded_search_payload_and_applies_filters() -> None:
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/us/en/search-results"
        assert request.url.params["keywords"] == "data engineer"
        assert request.url.params["location"] == "Boston"
        assert request.url.params["category"] == "Technology"
        assert "s" not in request.url.params
        offset = int(request.url.params.get("from", "0"))
        offsets.append(offset)
        ids = ["R1", "R2"] if offset == 0 else ["R3"]
        return httpx.Response(200, text=phenom_page(search_ddo(ids, 3)))

    extractor = make_extractor(
        httpx.Client(transport=httpx.MockTransport(handler))
    )

    references = extractor.search_jobs(
        "data engineer", "Boston", facets={"category": "Technology"}
    )

    assert references == ["R1", "R2", "R3"]
    assert offsets == [0, 2]


def test_phenom_extracts_canonical_job_and_preserves_raw_page(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/us/en/search-results":
            return httpx.Response(200, text=phenom_page(search_ddo(["R123"], 1)))
        assert request.url.path == "/us/en/job/R123/data-engineer-r123"
        return httpx.Response(200, text=phenom_page(detail_ddo()))

    storage = BronzeStorage(tmp_path)
    extractor = make_extractor(
        httpx.Client(transport=httpx.MockTransport(handler)),
        bronze_storage=storage,
    )

    result = extractor.extract("data")
    posting = result.postings[0]
    raw_files = list((tmp_path / "phenom" / "acme" / "R123").glob("*.json"))

    assert result.references_found == 1
    assert result.raw_records_saved == 1
    assert len(raw_files) == 1
    storage_key = raw_files[0].relative_to(tmp_path).with_suffix("").as_posix()
    raw_payload = storage.load(storage_key)
    assert isinstance(raw_payload, dict)
    assert "phApp.ddo" in str(raw_payload["raw_html"])
    assert posting.source.external_id == "R123"
    assert posting.company.name == "Acme, Inc."
    assert posting.position.title == "Senior Data Engineer"
    assert posting.position.family == "Technology"
    assert posting.location.city == "Boston"
    assert posting.location.region == "Massachusetts"
    assert posting.location.country == "United States"
    assert posting.location.latitude == 42.3601
    assert posting.location.workplace_type == WorkplaceType.HYBRID
    assert posting.employment_type == EmploymentType.FULL_TIME
    assert posting.metadata.posted_at is not None
    assert posting.metadata.posted_at.isoformat() == "2026-08-20"
    assert posting.skills.explicit == ["python", "sql", "aws"]
    assert posting.description.responsibilities == ["Build reliable data pipelines."]


def test_phenom_bronze_route_processes_into_silver(tmp_path: Path) -> None:
    from extractors import ExtractorRegistry
    from pipeline import BronzeToSilverRunner

    targets = ExtractorTargetsConfig.model_validate(
        {
            "targets": [
                {
                    "name": "acme-phenom",
                    "source": "phenom",
                    "host": "https://careers.acme.example",
                    "tenant": "ACMEUS",
                    "site": "acme",
                    "company_name": "Acme, Inc.",
                }
            ]
        }
    )
    bronze = BronzeStorage(tmp_path / "bronze")
    silver = SilverStorage(tmp_path / "silver")
    payload = detail_job()
    payload["url"] = "https://careers.acme.example/us/en/job/R123/senior-data-engineer"
    payload["raw_html"] = phenom_page(detail_ddo())
    bronze.save("phenom/acme/R123/version-1", payload)
    registry = ExtractorRegistry(
        config=Config(),
        targets=targets,
        bronze_storage=bronze,
        checkpoint_manager=CheckpointManager(tmp_path / "checkpoints"),
    )

    result = BronzeToSilverRunner(registry, silver_storage=silver).run()

    assert result.records_found == 1
    assert result.records_processed == 1
    assert result.silver_created == 1
    assert result.failed == 0


def test_phenom_rejects_malformed_ddo_and_cross_tenant_detail() -> None:
    malformed = make_extractor(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, text="<html>no ddo</html>")
            )
        )
    )
    try:
        malformed.search_jobs("")
    except ATSResponseError:
        pass
    else:
        raise AssertionError("missing DDO must fail fast")

    other_job = detail_job()
    other_job["refNum"] = "OTHER"
    cross_tenant = make_extractor(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    text=phenom_page(
                        {"jobDetail": {"data": {"job": other_job}}}
                    ),
                )
            )
        )
    )
    try:
        cross_tenant.fetch_job("R123")
    except ATSResponseError as exc:
        assert "another tenant" in str(exc)
    else:
        raise AssertionError("cross-tenant details must be rejected")
