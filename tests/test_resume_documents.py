"""Safe PDF/DOCX text extraction and text-quality scoring."""

from __future__ import annotations

import io
import zipfile

import pytest
from pypdf import PdfReader, PdfWriter

from models.candidate import ExtractionStatus, ResumeMediaType
from profiles.documents import (
    ResumeDocumentError,
    detect_media_type,
    extract_document_text,
    normalize_text,
)
from profiles.quality import assess_text_quality, repeated_line_ratio
from tests.support.resumes import (
    SAMPLE_RESUME,
    SCANNED_LIKE_TEXT,
    make_banded_pdf,
    make_docx,
    make_pdf,
)
from utils.config import ResumeIngestionConfig

LIMITS = ResumeIngestionConfig()


def test_pdf_and_docx_yield_the_same_lines() -> None:
    pdf = extract_document_text(make_pdf(SAMPLE_RESUME), LIMITS)
    docx = extract_document_text(make_docx(SAMPLE_RESUME), LIMITS)

    assert pdf.media_type == ResumeMediaType.PDF
    assert docx.media_type == ResumeMediaType.DOCX
    assert pdf.page_count == 1
    assert docx.page_count is None
    non_blank = [line for line in docx.text.split("\n") if line]
    assert pdf.text.split("\n") == non_blank
    assert "Data Analyst Intern | Sunshine Health Analytics" in docx.text
    assert not pdf.truncated


def test_type_is_detected_from_content_not_extension() -> None:
    assert detect_media_type(make_pdf("hello")) == ResumeMediaType.PDF
    assert detect_media_type(make_docx("hello")) == ResumeMediaType.DOCX
    with pytest.raises(ResumeDocumentError, match="only PDF and DOCX"):
        detect_media_type(b"GIF89a fake resume")
    plain_zip = io.BytesIO()
    with zipfile.ZipFile(plain_zip, "w") as archive:
        archive.writestr("notes.txt", "hello")
    with pytest.raises(ResumeDocumentError, match="not a Word document"):
        detect_media_type(plain_zip.getvalue())
    with pytest.raises(ResumeDocumentError, match="damaged"):
        detect_media_type(b"PK\x03\x04 not really a zip")


@pytest.mark.parametrize(
    ("content", "limits", "message"),
    [
        (b"", LIMITS, "empty"),
        (make_pdf(SAMPLE_RESUME), ResumeIngestionConfig(max_file_size_bytes=100), "limit"),
        (
            make_pdf(SAMPLE_RESUME, lines_per_page=5),
            ResumeIngestionConfig(max_pdf_pages=2),
            "pages",
        ),
        (make_docx(SAMPLE_RESUME), ResumeIngestionConfig(max_docx_entries=1), "entries"),
    ],
)
def test_limits_are_enforced(content: bytes, limits: ResumeIngestionConfig, message: str) -> None:
    with pytest.raises(ResumeDocumentError, match=message):
        extract_document_text(content, limits)


def test_encrypted_pdf_is_rejected() -> None:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(make_pdf(SAMPLE_RESUME))))
    writer.encrypt("secret")
    buffer = io.BytesIO()
    writer.write(buffer)

    with pytest.raises(ResumeDocumentError, match="password-protected"):
        extract_document_text(buffer.getvalue(), LIMITS)


def test_zip_bomb_and_dtd_documents_are_rejected() -> None:
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "<a>" + " " * 3_000_000 + "</a>")
    with pytest.raises(ResumeDocumentError, match="compression ratio"):
        extract_document_text(bomb.getvalue(), LIMITS)

    entity = io.BytesIO()
    with zipfile.ZipFile(entity, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>',
        )
    with pytest.raises(ResumeDocumentError, match="DTD"):
        extract_document_text(entity.getvalue(), LIMITS)


def test_long_text_is_truncated_and_flagged() -> None:
    document = extract_document_text(
        make_docx(SAMPLE_RESUME), ResumeIngestionConfig(max_extracted_characters=600)
    )
    checks, _, status = assess_text_quality(document.text, LIMITS, truncated=document.truncated)

    assert document.truncated
    assert len(document.text) == 600
    assert status == ExtractionStatus.NEEDS_ATTENTION
    assert not next(check for check in checks if check.name == "not_truncated").passed


def test_quality_passes_real_text_and_fails_scanned_text() -> None:
    _, score, status = assess_text_quality(SAMPLE_RESUME, LIMITS)
    _, scanned_score, scanned_status = assess_text_quality(SCANNED_LIKE_TEXT, LIMITS)
    _, garbled_score, garbled_status = assess_text_quality("�� #### %%%% " * 60, LIMITS)

    assert (score, status) == (1.0, ExtractionStatus.PASSED)
    assert scanned_status == ExtractionStatus.FAILED
    assert scanned_score < 0.75
    assert garbled_status == ExtractionStatus.FAILED
    assert garbled_score < 1.0


def test_clipped_band_forms_yield_one_copy_with_overlays_in_place() -> None:
    lines = SAMPLE_RESUME.split("\n")
    languages = lines.index("Languages: Python, SQL, R, Java")
    pdf = make_banded_pdf(
        SAMPLE_RESUME,
        hidden_line=1,
        overlays=(
            (1, 50, "Delray Beach, FL | jordan.rivera@example.com"),
            (languages, 400, ", Rust"),
        ),
    )

    document = extract_document_text(pdf, LIMITS)

    expected = [line for line in lines if line]
    expected[1] = "Delray Beach, FL | jordan.rivera@example.com"
    expected[expected.index("Languages: Python, SQL, R, Java")] += ", Rust"
    extracted = [line for line in document.text.split("\n") if line]
    assert extracted == [normalize_text(line) for line in expected]
    _, _, status = assess_text_quality(document.text, LIMITS)
    assert status == ExtractionStatus.PASSED


def test_repeated_content_needs_attention() -> None:
    assert repeated_line_ratio(SAMPLE_RESUME) == 0.0

    checks, _, status = assess_text_quality(SAMPLE_RESUME * 3, LIMITS)

    repeated = next(check for check in checks if check.name == "repeated_line_ratio")
    assert not repeated.passed
    assert repeated.value > 0.6
    assert status == ExtractionStatus.NEEDS_ATTENTION
