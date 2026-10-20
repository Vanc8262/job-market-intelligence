"""Resume ingestion, review rules, and the dashboard review API."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import ValidationError

from dashboard.app import create_server
from models.candidate import (
    ExtractionStatus,
    ResumeMediaType,
    ReviewApproval,
    ReviewDraft,
    ReviewSection,
    ReviewStatus,
)
from profiles import (
    ProfileRecordNotFoundError,
    ProfileRepositoryConflictError,
    ResumeDocumentError,
    ResumeIngestionService,
)
from profiles.documents import DocumentText
from tests.support.resumes import SAMPLE_RESUME, SCANNED_LIKE_TEXT, make_docx, make_pdf

ALL_SECTIONS = [section.value for section in ReviewSection]


@pytest.fixture
def service(tmp_path: Path) -> ResumeIngestionService:
    return ResumeIngestionService(private_root=tmp_path / "private")


def _write(tmp_path: Path, name: str, content: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_ingest_stores_private_files_and_dedupes(
    service: ResumeIngestionService, tmp_path: Path
) -> None:
    source = _write(tmp_path, "jordan.pdf", make_pdf(SAMPLE_RESUME))

    first = service.ingest(source, label="Data science v1", activate=True)
    again = service.ingest(source)

    assert first.extraction is not None
    assert first.extraction.status == ExtractionStatus.PASSED
    assert first.activated
    assert first.extraction.facts.section_counts()["experience"] == 2
    folder = service.private_root / first.resume_version.storage_key
    assert (folder / "original.pdf").read_bytes() == source.read_bytes()
    assert "Sunshine Health Analytics" in (folder / "extracted.txt").read_text(encoding="utf-8")
    assert service.profile().active_resume_version_id == first.resume_version.resume_version_id
    assert again.duplicate and not again.activated
    assert again.resume_version.resume_version_id == first.resume_version.resume_version_id
    assert len(service.repository.list_resume_versions(service.profile_id)) == 1
    assert "SUMMARY" not in json.dumps(first.summary())


def test_failed_quality_is_stored_but_never_activated(
    service: ResumeIngestionService, tmp_path: Path
) -> None:
    result = service.ingest(
        _write(tmp_path, "scan.pdf", make_pdf(SCANNED_LIKE_TEXT)), activate=True
    )

    assert result.extraction is not None
    assert result.extraction.status == ExtractionStatus.FAILED
    assert not result.activated
    assert service.profile().active_resume_version_id is None
    with pytest.raises(ProfileRepositoryConflictError, match="failed quality"):
        service.repository.save_review_draft(
            result.extraction.extraction_id,
            ReviewDraft(revision=1, facts=result.extraction.facts),
            profile_id=service.profile_id,
        )


def test_unsupported_and_oversized_files_are_rejected(
    service: ResumeIngestionService, tmp_path: Path
) -> None:
    with pytest.raises(ResumeDocumentError, match="only PDF and DOCX"):
        service.ingest(_write(tmp_path, "resume.pdf", b"not a pdf at all"))
    huge = _write(tmp_path, "huge.pdf", b"%PDF-" + b"0" * (service.limits.max_file_size_bytes + 1))
    with pytest.raises(ResumeDocumentError, match="limit"):
        service.ingest(huge)
    assert service.repository.list_resume_versions(service.profile_id) == []


def test_review_drafts_conflicts_and_approval(
    service: ResumeIngestionService, tmp_path: Path
) -> None:
    result = service.ingest(_write(tmp_path, "jordan.docx", make_docx(SAMPLE_RESUME)))
    assert result.extraction is not None
    extraction = result.extraction
    facts = extraction.facts.model_copy(deep=True)
    facts.skills = [skill for skill in facts.skills if skill.canonical != "R"]

    saved = service.repository.save_review_draft(
        extraction.extraction_id,
        ReviewDraft(revision=1, facts=facts, approved_sections=[ReviewSection.SKILLS]),
        profile_id=service.profile_id,
    )
    assert saved.revision == 2
    assert saved.review_status == ReviewStatus.PENDING
    assert "R" not in {skill.canonical for skill in saved.facts.skills}

    with pytest.raises(ProfileRepositoryConflictError, match="out of date"):
        service.repository.save_review_draft(
            extraction.extraction_id,
            ReviewDraft(revision=1, facts=facts),
            profile_id=service.profile_id,
        )
    with pytest.raises(ValidationError, match="missing: experience"):
        ReviewApproval(revision=2, facts=facts, approved_sections=[ReviewSection.SKILLS])
    with pytest.raises(ProfileRecordNotFoundError):
        service.repository.save_review_draft(
            extraction.extraction_id,
            ReviewDraft(revision=2, facts=facts),
            profile_id="someone-else",
        )

    approved = service.repository.approve_review(
        extraction.extraction_id,
        ReviewApproval(revision=2, facts=facts, approved_sections=list(ReviewSection)),
        profile_id=service.profile_id,
    )
    assert approved.review_status == ReviewStatus.APPROVED
    assert approved.approved_at is not None
    assert service.repository.latest_approved_extraction(result.resume_version.resume_version_id)
    with pytest.raises(ProfileRepositoryConflictError, match="already approved"):
        service.repository.save_review_draft(
            extraction.extraction_id,
            ReviewDraft(revision=3, facts=facts),
            profile_id=service.profile_id,
        )


def test_reextract_adds_a_new_pending_extraction(
    service: ResumeIngestionService, tmp_path: Path
) -> None:
    result = service.ingest(_write(tmp_path, "jordan.pdf", make_pdf(SAMPLE_RESUME)))
    version_id = result.resume_version.resume_version_id

    fresh = service.reextract(version_id)

    extractions = service.repository.list_extractions(version_id)
    assert len(extractions) == 2
    assert fresh.review_status == ReviewStatus.PENDING
    assert fresh.text_sha256 == extractions[1].text_sha256


def test_reextract_keeps_the_text_each_extraction_was_parsed_from(
    service: ResumeIngestionService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = service.ingest(_write(tmp_path, "jordan.pdf", make_pdf(SAMPLE_RESUME)))
    version_id = first.resume_version.resume_version_id
    assert first.extraction is not None
    original = service.extracted_text(version_id)
    folder = service.private_root / first.resume_version.storage_key
    # Data stored before per-extraction text files existed has only extracted.txt.
    (folder / f"extracted-{first.extraction.text_sha256[:16]}.txt").unlink()
    changed = DocumentText(ResumeMediaType.PDF, original + "\nAWARDS\nDean's List", 1, False)
    monkeypatch.setattr("profiles.service.extract_document_text", lambda *_: changed)

    fresh = service.reextract(version_id)

    assert service.extracted_text(version_id, first.extraction.text_sha256) == original
    assert service.extracted_text(version_id, fresh.text_sha256) == changed.text
    assert service.extracted_text(version_id) == changed.text
    with pytest.raises(FileNotFoundError):
        service.extracted_text(version_id, "0" * 64)


# Dashboard API -----------------------------------------------------------------------


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[str, ResumeIngestionService]]:
    private = tmp_path / "private"
    ingest = ResumeIngestionService(private_root=private)
    ingest.ingest(_write(tmp_path, "jordan.pdf", make_pdf(SAMPLE_RESUME)), activate=True)
    server = create_server(
        port=0,
        mart_path=tmp_path / "missing-mart.json",
        tracking_path=tmp_path / "tracking.json",
        gold_root=tmp_path / "gold",
        private_root=private,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", ingest
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _call(
    url: str, method: str = "GET", body: object | None = None, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, object]]:
    data = None if body is None else json.dumps(body).encode()
    request = Request(
        url,
        data=data,
        method=method,
        headers=headers if headers is not None else {"Content-Type": "application/json"},
    )
    try:
        with urlopen(request) as response:  # noqa: S310
            return int(response.status), json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_review_api_flow(api: tuple[str, ResumeIngestionService]) -> None:
    base, _ = api
    status, profile = _call(f"{base}/api/candidate-profile")
    assert status == 200
    versions = profile["resume_versions"]
    assert isinstance(versions, list) and versions[0]["is_active"] is True

    status, listing = _call(f"{base}/api/resume-reviews")
    assert status == 200 and listing["pending_count"] == 1
    items = listing["items"]
    assert isinstance(items, list)
    extraction_id = items[0]["extraction_id"]
    assert "text" not in items[0] and "facts" not in items[0]

    status, detail = _call(f"{base}/api/resume-reviews/{extraction_id}")
    assert status == 200
    assert "Sunshine Health Analytics" in str(detail["text"])
    extraction = detail["extraction"]
    assert isinstance(extraction, dict)
    facts = extraction["facts"]

    url = f"{base}/api/resume-reviews/{extraction_id}"
    status, draft = _call(
        url, "PATCH", {"revision": 1, "facts": facts, "approved_sections": ["skills"]}
    )
    assert status == 200 and draft["revision"] == 2

    status, stale = _call(url, "PATCH", {"revision": 1, "facts": facts})
    assert status == 409 and "out of date" in str(stale["error"])

    status, incomplete = _call(
        url, "PUT", {"revision": 2, "facts": facts, "approved_sections": ["skills"]}
    )
    assert status == 400 and "missing" in str(incomplete["error"])

    status, approved = _call(
        url, "PUT", {"revision": 2, "facts": facts, "approved_sections": ALL_SECTIONS}
    )
    assert status == 200 and approved["review_status"] == "approved"

    status, _ = _call(f"{base}/api/resume-reviews/ex-does-not-exist")
    assert status == 404


def test_review_api_security_and_validation(api: tuple[str, ResumeIngestionService]) -> None:
    base, _ = api
    _, listing = _call(f"{base}/api/resume-reviews")
    items = listing["items"]
    assert isinstance(items, list)
    url = f"{base}/api/resume-reviews/{items[0]['extraction_id']}"

    status, _ = _call(url, "GET", headers={"Host": "attacker.example"})
    assert status == 403
    status, _ = _call(
        url,
        "PATCH",
        {"revision": 1, "facts": {}},
        headers={"Content-Type": "application/json", "Origin": "https://attacker.example"},
    )
    assert status == 403
    status, _ = _call(url, "PATCH", {"revision": 1}, headers={"Content-Type": "text/plain"})
    assert status == 415
    status, invalid = _call(
        url, "PATCH", {"revision": 1, "facts": {"skills": [{"name": "", "source": "reviewer"}]}}
    )
    assert status == 400 and "skills" in str(invalid["error"])
    status, _ = _call(f"{base}/api/resume-reviews/..%2F..%2Fsecrets", "PATCH", {"revision": 1})
    assert status in {400, 404}


def test_tracking_accepts_only_known_resume_versions(
    api: tuple[str, ResumeIngestionService],
) -> None:
    base, ingest = api
    version_id = ingest.repository.list_resume_versions(ingest.profile_id)[0].resume_version_id

    status, saved = _call(
        f"{base}/api/application-tracking/job-1", "PUT", {"resume_version_id": version_id}
    )
    assert status == 200 and saved["resume_version_id"] == version_id

    status, error = _call(
        f"{base}/api/application-tracking/job-1", "PUT", {"resume_version_id": "rv-made-up"}
    )
    assert status == 400 and "not found" in str(error["error"])
