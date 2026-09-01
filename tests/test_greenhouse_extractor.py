"""Tests for the Greenhouse Job Board API extractor."""

from pathlib import Path

import httpx

from extractors.greenhouse import GreenhouseExtractor
from models.job_posting import EmploymentType, SourceType, WorkplaceType
from storage import BronzeStorage


def greenhouse_job(job_id: int = 101, *, title: str = "Senior Data Scientist") -> dict:
    return {
        "id": job_id,
        "title": title,
        "updated_at": "2026-08-20T12:00:00Z",
        "location": {"name": "Remote - United States"},
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "language": "en",
        "content": "&lt;p&gt;Build machine learning systems &amp;amp; data products.&lt;/p&gt;",
        "departments": [{"id": 1, "name": "Data"}],
        "offices": [{"id": 2, "name": "United States"}],
        "metadata": [{"name": "Employment Type", "value": "Full-time"}],
    }


def test_search_filters_jobs_and_reuses_list_payload() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/boards/acme"):
            return httpx.Response(200, json={"name": "Acme, Inc."})
        return httpx.Response(
            200,
            json={
                "jobs": [
                    greenhouse_job(),
                    greenhouse_job(102, title="Account Executive"),
                ],
                "meta": {"total": 2},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    extractor = GreenhouseExtractor("acme", client=client, sleep=lambda _seconds: None)

    references = extractor.search_jobs("senior", "remote", department="data", language="EN")
    raw_job = extractor.fetch_job(references[0])

    assert references == ["101"]
    assert raw_job["title"] == "Senior Data Scientist"
    assert len(requests) == 2
    assert requests[1].url.params["content"] == "true"


def test_parse_job_builds_canonical_record() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(500)))
    extractor = GreenhouseExtractor("acme", company_name="Acme, Inc.", client=client)

    posting = extractor.parse_job(greenhouse_job())

    assert posting.source.source_type == SourceType.ATS
    assert posting.source.external_id == "101"
    assert posting.company.name == "Acme, Inc."
    assert posting.position.family == "Data"
    assert posting.location.workplace_type == WorkplaceType.REMOTE
    assert posting.employment_type == EmploymentType.FULL_TIME
    assert posting.description.clean_text == "Build machine learning systems & data products."
    assert posting.metadata.job_id
    assert posting.metadata.content_hash
    assert posting.metadata.duplicate_hash


def test_extract_saves_versioned_raw_payload_to_bronze(tmp_path: Path) -> None:
    job = greenhouse_job()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/boards/acme"):
            return httpx.Response(200, json={"name": "Acme, Inc."})
        return httpx.Response(200, json={"jobs": [job], "meta": {"total": 1}})

    storage = BronzeStorage(tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    extractor = GreenhouseExtractor(
        "acme", bronze_storage=storage, client=client, sleep=lambda _seconds: None
    )

    first = extractor.extract("data scientist")
    second = extractor.extract("data scientist")
    raw_files = list((tmp_path / "greenhouse" / "acme" / "101").glob("*.json"))

    assert first.succeeded == 1
    assert first.raw_records_saved == 1
    assert second.succeeded == 1
    assert second.raw_records_saved == 0
    assert len(raw_files) == 1
    assert storage.load(extractor.raw_storage_key(job)) == job


def test_fetch_job_uses_detail_endpoint_when_not_cached() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        return httpx.Response(200, json=greenhouse_job())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    extractor = GreenhouseExtractor(
        "acme", company_name="Acme, Inc.", client=client, sleep=lambda _seconds: None
    )

    assert extractor.fetch_job("101")["id"] == 101
    assert requested_paths == ["/v1/boards/acme/jobs/101"]


def test_transient_server_error_is_retried() -> None:
    attempts = 0
    waits: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json=greenhouse_job())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    extractor = GreenhouseExtractor(
        "acme",
        company_name="Acme, Inc.",
        client=client,
        sleep=waits.append,
    )

    assert extractor.fetch_job("101")["id"] == 101
    assert attempts == 2
    assert waits


def test_unknown_search_filter_fails_fast() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
    extractor = GreenhouseExtractor("acme", company_name="Acme, Inc.", client=client)

    try:
        extractor.search_jobs("data", unsupported="value")
    except ValueError as exc:
        assert "unsupported" in str(exc).casefold()
    else:
        raise AssertionError("unknown Greenhouse filters must be rejected")
