"""Contract tests for Lever, Ashby, and Workday extractors."""

import json
from pathlib import Path

import httpx

from extractors import AshbyExtractor, LeverExtractor, WorkdayExtractor
from models.job_posting import EmploymentType, PayPeriod, WorkplaceType
from storage import BronzeStorage


def lever_job() -> dict:
    return {
        "id": "lever-101",
        "text": "Senior Data Engineer",
        "createdAt": 1787227200000,
        "categories": {
            "location": "New York, NY",
            "commitment": "Full-time",
            "department": "Engineering",
            "team": "Data Platform",
        },
        "country": "US",
        "workplaceType": "hybrid",
        "description": "<p>Build reliable data products.</p>",
        "descriptionPlain": "Build reliable data products.",
        "hostedUrl": "https://jobs.lever.co/acme/lever-101",
        "applyUrl": "https://jobs.lever.co/acme/lever-101/apply",
        "salaryRange": {"currency": "USD", "interval": "year", "min": 150000, "max": 190000},
        "salaryDescriptionPlain": "$150,000-$190,000 per year",
    }


def ashby_job() -> dict:
    return {
        "id": "ashby-101",
        "title": "Machine Learning Engineer",
        "location": "Remote - United States",
        "department": "Engineering",
        "team": "Machine Learning",
        "isListed": True,
        "isRemote": True,
        "workplaceType": "Remote",
        "descriptionHtml": "<p>Deploy machine learning models.</p>",
        "descriptionPlain": "Deploy machine learning models.",
        "publishedAt": "2026-08-20T12:00:00Z",
        "employmentType": "FullTime",
        "address": {"postalAddress": {"addressCountry": "USA"}},
        "jobUrl": "https://jobs.ashbyhq.com/acme/ashby-101",
        "applyUrl": "https://jobs.ashbyhq.com/acme/ashby-101/application",
        "compensation": {
            "scrapeableCompensationSalarySummary": "$160K - $200K",
            "summaryComponents": [
                {
                    "compensationType": "Salary",
                    "interval": "1 YEAR",
                    "currencyCode": "USD",
                    "minValue": 160000,
                    "maxValue": 200000,
                }
            ],
        },
    }


def workday_summary() -> dict:
    return {
        "title": "Data Scientist",
        "externalPath": "/job/New-York/Data-Scientist_R-101",
        "locationsText": "New York, NY",
        "postedOn": "Posted Today",
    }


def workday_detail() -> dict:
    return {
        "externalPath": "/job/New-York/Data-Scientist_R-101",
        "jobPostingInfo": {
            "title": "Data Scientist",
            "jobReqId": "R-101",
            "jobDescription": "<p>Build statistical models.</p>",
            "location": "New York, NY",
            "locationCountry": "United States",
            "remoteType": "Hybrid",
            "timeType": "Full time",
            "jobFamily": "Data Science",
            "startDate": "2026-08-20",
        },
    }


def test_lever_extracts_filters_and_saves_bronze(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["mode"] == "json"
        return httpx.Response(200, json=[lever_job()])

    extractor = LeverExtractor(
        "acme",
        company_name="Acme, Inc.",
        bronze_storage=BronzeStorage(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    result = extractor.extract("data engineer", "New York", department="engineering")
    posting = result.postings[0]

    assert result.succeeded == 1
    assert result.raw_records_saved == 1
    assert posting.employment_type == EmploymentType.FULL_TIME
    assert posting.location.workplace_type == WorkplaceType.HYBRID
    assert posting.salary.pay_period == PayPeriod.YEARLY
    assert posting.salary.max_amount == 190000


def test_ashby_extracts_compensation_and_skips_unlisted() -> None:
    hidden = {**ashby_job(), "id": "hidden", "isListed": False}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["includeCompensation"] == "true"
        return httpx.Response(200, json={"apiVersion": "1", "jobs": [ashby_job(), hidden]})

    extractor = AshbyExtractor(
        "acme",
        company_name="Acme, Inc.",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    references = extractor.search_jobs("machine learning", "remote", team="machine")
    posting = extractor.parse_job(extractor.fetch_job(references[0]))

    assert references == ["ashby-101"]
    assert posting.location.workplace_type == WorkplaceType.REMOTE
    assert posting.employment_type == EmploymentType.FULL_TIME
    assert posting.salary.min_amount == 160000
    assert posting.salary.pay_period == PayPeriod.YEARLY


def test_ashby_uses_job_url_as_fallback_identifier() -> None:
    job = ashby_job()
    del job["id"]
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"apiVersion": "1", "jobs": [job]})
        )
    )
    extractor = AshbyExtractor("acme", client=client, sleep=lambda _seconds: None)

    assert extractor.search_jobs("") == ["ashby-101"]
    assert extractor.parse_job(job).source.external_id == "ashby-101"


def test_workday_paginates_search_and_passes_native_facets() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        offset = body["offset"]
        summary = {**workday_summary(), "externalPath": f"/job/example/R-{offset + 1}"}
        return httpx.Response(200, json={"jobPostings": [summary], "total": 2})

    extractor = WorkdayExtractor(
        "https://acme.wd5.myworkdayjobs.com",
        "acme",
        "External",
        page_size=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    references = extractor.search_jobs("data", facets={"jobFamilyGroup": ["abc"]})

    assert references == ["/job/example/R-1", "/job/example/R-2"]
    assert [body["offset"] for body in bodies] == [0, 1]
    assert bodies[0]["appliedFacets"] == {"jobFamilyGroup": ["abc"]}


def test_workday_extracts_detail_and_saves_bronze(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"jobPostings": [workday_summary()], "total": 1})
        return httpx.Response(200, json=workday_detail())

    extractor = WorkdayExtractor(
        "https://acme.wd5.myworkdayjobs.com",
        "acme",
        "External",
        company_name="Acme, Inc.",
        bronze_storage=BronzeStorage(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    result = extractor.extract("data scientist", "New York")
    posting = result.postings[0]

    assert result.succeeded == 1
    assert result.raw_records_saved == 1
    assert posting.source.external_id == "R-101"
    assert posting.position.family == "Data Science"
    assert posting.employment_type == EmploymentType.FULL_TIME
    assert posting.location.workplace_type == WorkplaceType.HYBRID
    assert posting.description.clean_text == "Build statistical models."


def test_workday_rejects_non_https_host() -> None:
    try:
        WorkdayExtractor("http://example.com", "tenant", "site")
    except ValueError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("Workday must reject non-HTTPS hosts")


def test_shared_ats_http_retries_transient_errors() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json=[lever_job()])

    extractor = LeverExtractor(
        "acme",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    assert extractor.search_jobs("data") == ["lever-101"]
    assert attempts == 2
