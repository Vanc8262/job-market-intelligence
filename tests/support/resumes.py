"""Synthetic resume fixtures: realistic text, plus real PDF and DOCX files built from it.

No real person's resume is used anywhere in the test suite.
"""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

SAMPLE_RESUME = """Jordan Rivera
Boca Raton, FL | jordan.rivera@example.com | github.com/jrivera-example

SUMMARY
Data science student who builds reliable data pipelines and statistical models.

TECHNICAL SKILLS
Languages: Python, SQL, R, Java
Data & ML: pandas, NumPy, scikit-learn, PyTorch, statistics
Cloud & Tools: AWS, Docker, Git, Tableau, Apache Airflow

EXPERIENCE
Data Analyst Intern | Sunshine Health Analytics | Fort Lauderdale, FL    May 2025 - Aug 2025
• Built Python and SQL pipelines that cut weekly reporting time by 40%.
• Trained scikit-learn churn models and presented results to 3 product teams.
Coastal Logistics Co.
Software Developer    Jan 2023 - Present
• Maintained REST APIs in Java and PostgreSQL for 12 warehouse sites.
• Containerized services with Docker and deployed them to AWS.

EDUCATION
Florida Atlantic University, Boca Raton, FL
B.S. Data Science and Analytics    Expected May 2027
Palm Beach State College
Associate in Arts    May 2023

PROJECTS
Job Market Tracker | Python, DuckDB
• Collects public job postings and ranks them against a profile.
• Uses pandas for cleaning and Airflow for scheduling.
Housing Price Model - 2024
Bayesian regression of county housing prices with PyMC.
Technologies: Python, statistics
"""

SCANNED_LIKE_TEXT = "Jordan Rivera\n\n\n"


def make_docx(text: str) -> bytes:
    """Return a minimal valid DOCX whose paragraphs are the lines of ``text``."""
    paragraphs = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>'
        for line in text.split("\n")
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{paragraphs}</w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def make_pdf(text: str, *, lines_per_page: int = 60) -> bytes:
    """Return a real text PDF (Helvetica, WinAnsi) with one text line per resume line."""
    lines = [_pdf_safe(line) for line in text.split("\n")]
    pages = [
        lines[index : index + lines_per_page] for index in range(0, len(lines), lines_per_page)
    ]
    page_ids = [4 + 2 * index for index in range(len(pages))]
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for index, page_lines in enumerate(pages):
        commands = ["BT", "/F1 10 Tf", "12 TL", "50 760 Td"]
        commands.extend(f"({line}) Tj T*" for line in page_lines)
        commands.append("ET")
        stream = "\n".join(commands).encode("cp1252")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {page_ids[index] + 1} 0 R >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    output = io.BytesIO()
    output.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(output.tell())
        output.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = output.tell()
    output.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        output.write(f"{offset:010d} 00000 n \n".encode())
    output.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return output.getvalue()


def _pdf_safe(line: str) -> str:
    value = line.encode("cp1252", errors="replace").decode("cp1252")
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
