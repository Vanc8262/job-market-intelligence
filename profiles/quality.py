"""Text-quality checks that decide whether an extraction is worth reviewing."""

from __future__ import annotations

from models.candidate import ExtractionStatus, QualityCheck
from utils.config import ResumeIngestionConfig


def assess_text_quality(
    text: str, limits: ResumeIngestionConfig, *, truncated: bool = False
) -> tuple[list[QualityCheck], float, ExtractionStatus]:
    """Score extracted text and classify it as passed, needs attention, or failed.

    A scanned (image-only) PDF yields almost no text and fails; a garbled encoding
    shows up as replacement characters or a low share of letters and digits. Repeated
    lines mean the document was read more than once (for example stacked copies of a
    page), which would give every fact duplicate evidence.
    """
    characters = len(text)
    words = len(text.split())
    visible = [character for character in text if not character.isspace()]
    alphanumeric_ratio = (
        sum(character.isalnum() for character in visible) / len(visible) if visible else 0.0
    )
    replacement_ratio = text.count("�") / characters if characters else 0.0
    checks = [
        _minimum("characters", characters, limits.minimum_characters, "Enough text was extracted"),
        _minimum("words", words, limits.minimum_words, "Enough words were extracted"),
        _minimum(
            "alphanumeric_ratio",
            round(alphanumeric_ratio, 4),
            limits.minimum_alphanumeric_ratio,
            "Text is mostly letters and digits, not symbols",
        ),
        _maximum(
            "replacement_ratio",
            round(replacement_ratio, 4),
            limits.maximum_replacement_ratio,
            "Few unreadable characters",
        ),
        _maximum(
            "repeated_line_ratio",
            round(repeated_line_ratio(text), 4),
            limits.maximum_repeated_line_ratio,
            "Content is not repeated",
        ),
        QualityCheck(
            name="not_truncated",
            passed=not truncated,
            value=0.0 if truncated else 1.0,
            threshold=1.0,
            comparison="min",
            message="Text fit within the extraction limit",
        ),
    ]
    score = round(sum(check.passed for check in checks) / len(checks), 4)
    hard_failure = not checks[0].passed or not checks[1].passed
    if hard_failure:
        status = ExtractionStatus.FAILED
    elif score >= limits.minimum_quality_score and all(check.passed for check in checks):
        status = ExtractionStatus.PASSED
    elif score >= limits.minimum_quality_score:
        status = ExtractionStatus.NEEDS_ATTENTION
    else:
        status = ExtractionStatus.FAILED
    return checks, score, status


def repeated_line_ratio(text: str) -> float:
    """Return the share of substantial lines (four or more words) that repeat earlier ones.

    Short lines such as a city or an employer name legitimately recur on a resume, so
    they are ignored; fewer than five substantial lines is too little to judge.
    """
    lines = [" ".join(line.split()).casefold() for line in text.splitlines()]
    substantial = [line for line in lines if len(line.split()) >= 4]
    if len(substantial) < 5:
        return 0.0
    return 1 - len(set(substantial)) / len(substantial)


def _minimum(name: str, value: float, threshold: float, message: str) -> QualityCheck:
    return QualityCheck(
        name=name,
        passed=value >= threshold,
        value=float(value),
        threshold=float(threshold),
        comparison="min",
        message=message,
    )


def _maximum(name: str, value: float, threshold: float, message: str) -> QualityCheck:
    return QualityCheck(
        name=name,
        passed=value <= threshold,
        value=float(value),
        threshold=float(threshold),
        comparison="max",
        message=message,
    )


__all__ = ["assess_text_quality", "repeated_line_ratio"]
