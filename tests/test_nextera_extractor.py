"""Contract tests for the NextEra Energy career-site extractor."""

from pathlib import Path

import httpx

from extractors import NextEraExtractor
from models.job_posting import EmploymentType, WorkplaceType
from storage import BronzeStorage


def search_html(references: list[tuple[str, str]]) -> str:
    """Return a minimal server-rendered NextEra search page."""
    links = "".join(
        f'<a class="jobTitle-link" href="{reference}">{title}</a>'
        f'<a class="jobTitle-link" href="{reference}">{title}</a>'
        for reference, title in references
    )
    return f"<html><body><div id='searchresults'>{links}</div></body></html>"


def detail_html() -> str:
    """Return a representative NextEra detail page."""
    return """
    <html><body class="job-page">
      <h1>Sr Data Engineer</h1>
      <p class="jobDate">Date: Aug 2, 2026</p>
      <p class="jobLocation">Location(s): Juno Beach, FL, US, 33408</p>
      <p class="jobCompany">Company: NextEra Energy</p>
      <span class="jobdescription">
        <p><strong>Requisition ID:</strong> 94414</p>
        <p>This hybrid role builds data pipelines with Python, SQL, and AWS.</p>
        <p>Employee Group: Exempt</p>
        <p>Employee Type: Full Time</p>
        <p>Job Category: Science, Research, and Technology</p>
        <p>Organization: NextEra Energy Resources</p>
        <p>Relocation Provided: No</p>
      </span>
    </body></html>
    """


def test_nextera_paginates_and_deduplicates_responsive_links() -> None:
    starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("startrow", "0"))
        starts.append(start)
        count = 25 if start == 0 else 1
        references = [
            (f"/job/City/Data-Engineer-{start + index}/{1000 + start + index}/", "Data")
            for index in range(count)
        ]
        return httpx.Response(200, text=search_html(references))

    extractor = NextEraExtractor(
        "https://jobs.nexteraenergy.com",
        "nextera-energy",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    references = extractor.search_jobs("data", "Florida", distance=25)

    assert len(references) == 26
    assert len(set(references)) == 26
    assert starts == [0, 25]


def test_nextera_extracts_canonical_job_and_preserves_html_in_bronze(
    tmp_path: Path,
) -> None:
    reference = "/job/Juno-Beach/Sr-Data-Engineer-FL-33408/1380478800/"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/":
            assert request.url.params["q"] == "data engineer"
            assert request.url.params["locationsearch"] == "Florida"
            return httpx.Response(200, text=search_html([(reference, "Sr Data Engineer")]))
        assert request.url.path == reference
        return httpx.Response(200, text=detail_html())

    storage = BronzeStorage(tmp_path)
    extractor = NextEraExtractor(
        "https://jobs.nexteraenergy.com",
        "nextera-energy",
        company_name="NextEra Energy",
        bronze_storage=storage,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    result = extractor.extract("data engineer", "Florida")
    posting = result.postings[0]
    raw_files = list((tmp_path / "nextera" / "nextera-energy" / "94414").glob("*.json"))

    assert result.succeeded == 1
    assert result.raw_records_saved == 1
    assert len(raw_files) == 1
    storage_key = raw_files[0].relative_to(tmp_path).with_suffix("").as_posix()
    raw_payload = storage.load(storage_key)
    assert isinstance(raw_payload, dict)
    assert "job-page" in str(raw_payload.get("raw_html"))
    assert posting.source.external_id == "94414"
    assert posting.position.title == "Sr Data Engineer"
    assert posting.position.family == "Science, Research, and Technology"
    assert posting.location.raw == "Juno Beach, FL, US, 33408"
    assert posting.location.country == "United States"
    assert posting.location.workplace_type == WorkplaceType.HYBRID
    assert posting.employment_type == EmploymentType.FULL_TIME
    assert posting.metadata.posted_at is not None
    assert posting.metadata.posted_at.isoformat() == "2026-08-02"
    assert posting.metadata.tags == [
        "Science, Research, and Technology",
        "NextEra Energy Resources",
        "Exempt",
    ]
    assert posting.description.clean_text is not None
    assert "builds data pipelines" in posting.description.clean_text.casefold()


def test_nextera_rejects_external_or_non_job_references() -> None:
    extractor = NextEraExtractor(
        "https://jobs.nexteraenergy.com",
        "nextera-energy",
    )

    for reference in ("https://example.com/job/1", "/search/"):
        try:
            extractor.fetch_job(reference)
        except ValueError:
            pass
        else:
            raise AssertionError("NextEra must reject unsafe or non-job references")
    extractor.close()
