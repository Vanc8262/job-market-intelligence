"""Safe text extraction from untrusted PDF and DOCX resume files.

Every limit is enforced before or while reading, never after: file size, file type by
content (not by extension), PDF page count and encryption, and DOCX archive size,
entry count, compression ratio, and DTD-free XML.
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree

from pypdf import PageObject, PdfReader
from pypdf.errors import PdfReadError

from models.candidate import ResumeMediaType
from profiles.pdf_text import visible_page_text
from utils.config import ResumeIngestionConfig

WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DOCX_BODY = "word/document.xml"
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"[ \t  -​  　]+")
_BLANK_LINES = re.compile(r"\n{3,}")


class ResumeDocumentError(ValueError):
    """Raised when a file is not a safe, readable PDF or DOCX resume."""


@dataclass(frozen=True)
class DocumentText:
    """Normalized text and basic facts about one document."""

    media_type: ResumeMediaType
    text: str
    page_count: int | None
    truncated: bool


def detect_media_type(content: bytes) -> ResumeMediaType:
    """Identify a PDF or DOCX from its leading bytes and archive layout."""
    if content.startswith(b"%PDF-"):
        return ResumeMediaType.PDF
    if content.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if DOCX_BODY in archive.namelist():
                    return ResumeMediaType.DOCX
        except zipfile.BadZipFile as exc:
            raise ResumeDocumentError("file is a damaged ZIP archive") from exc
        raise ResumeDocumentError("ZIP file is not a Word document (no word/document.xml)")
    raise ResumeDocumentError("only PDF and DOCX resumes are supported")


def extract_document_text(content: bytes, limits: ResumeIngestionConfig) -> DocumentText:
    """Return normalized text from a PDF or DOCX within the configured limits."""
    if not content:
        raise ResumeDocumentError("file is empty")
    if len(content) > limits.max_file_size_bytes:
        raise ResumeDocumentError(
            f"file is {len(content):,} bytes; the limit is {limits.max_file_size_bytes:,}"
        )
    media_type = detect_media_type(content)
    if media_type == ResumeMediaType.PDF:
        raw, pages = _pdf_text(content, limits)
    else:
        raw, pages = _docx_text(content, limits), None
    text = normalize_text(raw)
    truncated = len(text) > limits.max_extracted_characters
    if truncated:
        text = text[: limits.max_extracted_characters]
    return DocumentText(media_type=media_type, text=text, page_count=pages, truncated=truncated)


def normalize_text(text: str) -> str:
    """Normalize Unicode, whitespace, bullets, and line endings without changing words."""
    value = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    value = _CONTROL_CHARACTERS.sub("", value)
    lines = [_SPACES.sub(" ", line).strip() for line in value.split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def _pdf_text(content: bytes, limits: ResumeIngestionConfig) -> tuple[str, int]:
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted:
            raise ResumeDocumentError("password-protected PDFs are not supported")
        page_count = len(reader.pages)
        if page_count == 0:
            raise ResumeDocumentError("PDF has no pages")
        if page_count > limits.max_pdf_pages:
            raise ResumeDocumentError(
                f"PDF has {page_count} pages; the limit is {limits.max_pdf_pages}"
            )
        parts: list[str] = []
        total = 0
        for page in reader.pages:
            page_text = _page_text(page)
            parts.append(page_text)
            total += len(page_text)
            if total > limits.max_extracted_characters * 2:
                break
    except ResumeDocumentError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
        raise ResumeDocumentError(f"PDF could not be read: {exc}") from exc
    return "\n".join(parts), page_count


def _page_text(page: PageObject) -> str:
    """Return visible page text, falling back to pypdf's plain extraction."""
    try:
        return visible_page_text(page)
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, ZeroDivisionError):
        return page.extract_text() or ""


def _docx_text(content: bytes, limits: ResumeIngestionConfig) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > limits.max_docx_entries:
                raise ResumeDocumentError(
                    f"DOCX has {len(entries)} entries; the limit is {limits.max_docx_entries}"
                )
            uncompressed = sum(entry.file_size for entry in entries)
            if uncompressed > limits.max_docx_uncompressed_bytes:
                raise ResumeDocumentError("DOCX expands beyond the uncompressed size limit")
            compressed = max(1, sum(entry.compress_size for entry in entries))
            if uncompressed / compressed > limits.max_docx_compression_ratio:
                raise ResumeDocumentError(
                    "DOCX compression ratio is suspicious (possible ZIP bomb)"
                )
            body = archive.read(DOCX_BODY)
    except ResumeDocumentError:
        raise
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise ResumeDocumentError(f"DOCX could not be read: {exc}") from exc
    if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        raise ResumeDocumentError("DOCX contains a DTD, which is not allowed")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise ResumeDocumentError(f"DOCX body is not valid XML: {exc}") from exc
    return "\n".join(_docx_paragraphs(root))


def _docx_paragraphs(root: ElementTree.Element) -> list[str]:
    """Return paragraph text in document order, including table cells."""
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{WORD_NAMESPACE}p"):
        pieces: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{WORD_NAMESPACE}t" and node.text:
                pieces.append(node.text)
            elif node.tag == f"{WORD_NAMESPACE}tab":
                pieces.append("\t")
            elif node.tag in {f"{WORD_NAMESPACE}br", f"{WORD_NAMESPACE}cr"}:
                pieces.append("\n")
        paragraphs.append("".join(pieces))
    return paragraphs


__all__ = [
    "DocumentText",
    "ResumeDocumentError",
    "detect_media_type",
    "extract_document_text",
    "normalize_text",
]
