"""Deterministic, evidence-first parser for resume text.

The parser proposes facts; a person approves them. Every fact records the exact span of
normalized resume text it came from (``text[start:end]``), so a reviewer and later
models can check it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from pydantic import ValidationError

from models.candidate import (
    EducationFact,
    Evidence,
    ExperienceFact,
    ExtractedFacts,
    ProjectFact,
    ReviewSection,
    SkillFact,
    SkillSource,
    YearMonth,
)
from utils.config import SkillsConfig

PARSER_NAME = "jmip-rules"
PARSER_VERSION = "1.0.0"

SECTION_HEADINGS: dict[str, tuple[str, ...]] = {
    ReviewSection.SKILLS.value: (
        "skills",
        "technical skills",
        "core skills",
        "key skills",
        "skills and tools",
        "skills & tools",
        "skills and technologies",
        "skills & technologies",
        "technologies",
        "technical proficiencies",
        "core competencies",
        "tools and technologies",
        "tools & technologies",
    ),
    ReviewSection.EXPERIENCE.value: (
        "experience",
        "work experience",
        "professional experience",
        "relevant experience",
        "employment",
        "employment history",
        "work history",
    ),
    ReviewSection.EDUCATION.value: ("education", "academic background", "education and training"),
    ReviewSection.PROJECTS.value: (
        "projects",
        "academic projects",
        "personal projects",
        "selected projects",
        "technical projects",
        "portfolio",
        "portfolio projects",
    ),
    "other": (
        "summary",
        "professional summary",
        "profile",
        "objective",
        "certifications",
        "certificates",
        "awards",
        "honors",
        "activities",
        "leadership",
        "volunteer",
        "volunteer experience",
        "interests",
        "publications",
        "languages",
        "references",
        "contact",
    ),
}
_HEADING_LOOKUP = {
    alias: section for section, aliases in SECTION_HEADINGS.items() for alias in aliases
}

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?"
    r"|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
)
_DATE = rf"(?:{_MONTH}\s+\d{{4}}|\d{{1,2}}/\d{{4}}|(?:19|20)\d{{2}})"
_RANGE = re.compile(
    rf"(?P<start>{_DATE})\s*(?:-|–|—|to)\s*(?P<end>{_DATE}|Present|Current|Now)\b",
    re.IGNORECASE,
)
_SINGLE_DATE = re.compile(rf"(?P<date>{_DATE})", re.IGNORECASE)
_BULLET = re.compile(r"^(?:[•▪●◦‣⁃∙·*–-]|o\s)\s*")
_LOCATION = re.compile(r"\b(?:Remote|[A-Z][A-Za-z.]+(?: [A-Z][A-Za-z.]+)*,\s*[A-Z]{2})\b")
_FIELD_SPLIT = re.compile(r"\s+(?:•|–|—|-|@|at)\s+|\s*\|\s*|\t+")
_TITLE_WORDS = re.compile(
    r"\b(?:engineer|developer|analyst|scientist|intern|internship|manager|assistant|"
    r"specialist|consultant|associate|lead|architect|administrator|technician|researcher|"
    r"coordinator|director|officer|programmer|designer|tutor|instructor|fellow|"
    r"representative|supervisor|clerk|cashier|owner|founder|volunteer)\b",
    re.IGNORECASE,
)
_INSTITUTION_WORDS = re.compile(
    r"\b(?:university|college|institute|school|academy|polytechnic)\b", re.IGNORECASE
)
_DEGREE = re.compile(
    r"(?<![A-Za-z])(?:Bachelor(?:'s)?|Master(?:'s)?|Doctor(?:ate)?|Ph\.?\s?D\.?|B\.S\.|B\.A\.|"
    r"M\.S\.|M\.A\.|BS|BA|MS|MA|MBA|A\.A\.|A\.S\.|AA|AS|Associate(?:'s)?|Diploma|"
    r"Certificate)(?![A-Za-z])[^,|\n]*"
)
_DEGREE_ABBREVIATION = re.compile(
    r"^(B\.S\.|B\.A\.|M\.S\.|M\.A\.|BS|BA|MS|MA|MBA|A\.A\.|A\.S\.|AA|AS|Ph\.?\s?D\.?)\s+(.+)$"
)
_TECH_LABEL = re.compile(r"^(?:technologies|tech stack|tech|tools|stack|built with)\s*:", re.I)
_LIST_SPLIT = re.compile(r"\s*(?:,|;|\||•|·)\s*")
_MAX_EVIDENCE = 5
_AMBIGUOUS_IN_BODY = {"go", "js", "ml", "torch", "r", "c"}


@dataclass(frozen=True)
class Line:
    """One non-blank line of normalized text with absolute character offsets."""

    text: str
    start: int

    @property
    def end(self) -> int:
        """Return the exclusive end offset."""
        return self.start + len(self.text)


@dataclass
class ParsedResume:
    """Proposed facts and parser warnings for one text."""

    facts: ExtractedFacts
    warnings: list[str] = field(default_factory=list)
    sections_found: list[str] = field(default_factory=list)


class SkillVocabulary:
    """Canonical skills and aliases, matched with boundaries that respect ``C++`` or ``C#``."""

    def __init__(self, skills: SkillsConfig, extra_terms: Iterable[str] = ()) -> None:
        """Index skills.yaml categories and aliases, plus extra terms (e.g. profile skills)."""
        self._canonical: dict[str, tuple[str, str | None]] = {}
        for category, names in skills.categories.items():
            for name in names:
                self._canonical[name.casefold()] = (name, category)
        for term in extra_terms:
            if term.strip():
                self._canonical.setdefault(term.strip().casefold(), (term.strip(), None))
        surfaces: dict[str, str] = {value[0]: value[0] for value in self._canonical.values()}
        for canonical, aliases in skills.aliases.items():
            for alias in aliases:
                surfaces[alias] = self._canonical[canonical.casefold()][0]
        self._lookup = {surface.casefold(): canonical for surface, canonical in surfaces.items()}
        self._loose = [
            (_pattern(surface, strict=False), canonical) for surface, canonical in surfaces.items()
        ]
        self._strict = [
            (_pattern(surface, strict=True), canonical)
            for surface, canonical in surfaces.items()
            if surface.casefold() not in _AMBIGUOUS_IN_BODY
        ]

    def lookup(self, item: str) -> tuple[str, str | None] | None:
        """Return ``(canonical, category)`` when ``item`` is exactly a known skill."""
        canonical = self._lookup.get(item.strip().casefold())
        return None if canonical is None else self._canonical[canonical.casefold()]

    def category(self, canonical: str) -> str | None:
        """Return the taxonomy category of a canonical skill, if it has one."""
        entry = self._canonical.get(canonical.casefold())
        return entry[1] if entry else None

    def find(self, text: str, *, body: bool) -> list[tuple[str, int, int]]:
        """Return non-overlapping ``(canonical, start, end)`` skill mentions, longest first.

        Body text uses strict matching: mixed-case terms must keep their case (so the verb
        "spark" is not Spark) and very short terms such as ``R`` or ``Go`` are skipped.
        """
        found: list[tuple[str, int, int]] = []
        for pattern, canonical in self._strict if body else self._loose:
            found.extend(
                (canonical, match.start(), match.end()) for match in pattern.finditer(text)
            )
        found.sort(key=lambda item: (item[1], -(item[2] - item[1])))
        accepted: list[tuple[str, int, int]] = []
        covered_until = -1
        for canonical, start, end in found:
            if start >= covered_until:
                accepted.append((canonical, start, end))
                covered_until = end
        return accepted


class ResumeParser:
    """Split resume text into sections and propose reviewable facts."""

    def __init__(self, vocabulary: SkillVocabulary) -> None:
        """Use ``vocabulary`` for skills and project technologies."""
        self.vocabulary = vocabulary

    def parse(self, text: str) -> ParsedResume:
        """Return proposed facts with exact evidence spans, plus warnings."""
        lines = _lines(text)
        sections = _sections(lines)
        warnings = [
            f"No {section.value} section heading was found"
            for section in ReviewSection
            if section.value not in sections
        ]
        facts = ExtractedFacts(
            skills=self._skills(text, sections),
            experience=self._experience(text, sections.get("experience", []), warnings),
            education=self._education(text, sections.get("education", [])),
            projects=self._projects(text, sections.get("projects", [])),
        )
        return ParsedResume(
            facts=facts,
            warnings=warnings,
            sections_found=[name for name in sections if name != "other"],
        )

    def _skills(self, text: str, sections: dict[str, list[Line]]) -> list[SkillFact]:
        facts: dict[str, SkillFact] = {}
        skill_lines = sections.get("skills", [])
        for line in skill_lines:
            content, offset = _strip_label(line)
            for item, start in _split_items(content, offset):
                if len(item.split()) > 4 or len(item) > 60:
                    continue
                match = self.vocabulary.lookup(item)
                if match is not None:
                    _add_skill(
                        facts,
                        match[0],
                        match[0],
                        match[1],
                        SkillSource.SKILLS_SECTION,
                        _evidence(text, "skills", start, start + len(item)),
                    )
                    continue
                inner = self.vocabulary.find(item, body=False)
                for canonical, low, high in inner:
                    _add_skill(
                        facts,
                        canonical,
                        canonical,
                        self.vocabulary.category(canonical),
                        SkillSource.SKILLS_SECTION,
                        _evidence(text, "skills", start + low, start + high),
                    )
                if not inner:
                    _add_skill(
                        facts,
                        item,
                        None,
                        None,
                        SkillSource.SKILLS_SECTION,
                        _evidence(text, "skills", start, start + len(item)),
                    )
        for canonical, start, end in self.vocabulary.find(text, body=True):
            if any(line.start <= start < line.end for line in skill_lines):
                continue
            _add_skill(
                facts,
                canonical,
                canonical,
                self.vocabulary.category(canonical),
                SkillSource.BODY,
                _evidence(text, _section_at(sections, start), start, end),
            )
        return list(facts.values())

    def _experience(
        self, text: str, lines: list[Line], warnings: list[str]
    ) -> list[ExperienceFact]:
        anchors = [index for index, line in enumerate(lines) if _RANGE.search(line.text)]
        if lines and not anchors:
            warnings.append("Experience section has no date ranges, so entries were not split")
            return []
        headers = [
            _header_start(lines, anchor, anchors[position - 1] + 1 if position else 0)
            for position, anchor in enumerate(anchors)
        ]
        entries: list[ExperienceFact] = []
        for position, anchor in enumerate(anchors):
            body_end = headers[position + 1] if position + 1 < len(anchors) else len(lines)
            header_lines = list(lines[headers[position] : anchor + 1])
            match = _RANGE.search(lines[anchor].text)
            assert match is not None
            header_text = " | ".join(
                _RANGE.sub(" ", line.text).strip(" |,–—-") for line in header_lines
            )
            title, employer, location = _split_role(header_text)
            body_start = anchor + 1
            if employer is None and body_start < body_end and _header_like(lines[body_start]):
                header_lines.append(lines[body_start])
                header_text = f"{header_text} | {lines[body_start].text}"
                title, employer, location = _split_role(header_text)
                body_start += 1
            if title is None:
                warnings.append(f"No job title recognized in {header_text[:80]!r}; review it")
                title, employer = (employer or "Untitled role"), None
            end_text = match.group("end")
            current = end_text.casefold() in {"present", "current", "now"}
            try:
                entries.append(
                    ExperienceFact(
                        title=title[:200],
                        employer=employer[:200] if employer else None,
                        location=location,
                        start=_parse_date(match.group("start")),
                        end=None if current else _parse_date(end_text),
                        current=current,
                        highlights=[
                            _BULLET.sub("", line.text).strip()[:500]
                            for line in lines[body_start:body_end]
                        ][:40],
                        evidence=_evidence(
                            text, "experience", header_lines[0].start, header_lines[-1].end
                        ),
                    )
                )
            except ValidationError as exc:
                warnings.append(
                    f"Skipped an experience entry with invalid dates: {exc.errors()[0]['msg']}"
                )
        return entries

    def _education(self, text: str, lines: list[Line]) -> list[EducationFact]:
        blocks: list[list[Line]] = []
        for line in lines:
            new_institution = bool(_INSTITUTION_WORDS.search(line.text)) and not _BULLET.match(
                line.text
            )
            if not blocks or (
                new_institution and any(_INSTITUTION_WORDS.search(item.text) for item in blocks[-1])
            ):
                blocks.append([line])
            else:
                blocks[-1].append(line)
        facts: list[EducationFact] = []
        for block in blocks:
            block_text = " | ".join(line.text for line in block)
            institution_line = next(
                (line for line in block if _INSTITUTION_WORDS.search(line.text)), None
            )
            if institution_line is None:
                continue
            institution = _clean(_first_fragment(institution_line.text, _INSTITUTION_WORDS))
            degree_match = _DEGREE.search(_SINGLE_DATE.sub(" ", block_text))
            degree, field_of_study = _split_degree(
                _clean(re.sub(r"\bexpected\b", "", degree_match.group(0), flags=re.I))
                if degree_match
                else None
            )
            dates = list(_SINGLE_DATE.finditer(block_text))
            facts.append(
                EducationFact(
                    institution=institution[:200] or institution_line.text[:200],
                    degree=degree,
                    field_of_study=field_of_study,
                    graduation=_parse_date(dates[-1].group("date")) if dates else None,
                    expected=bool(re.search(r"\b(?:expected|anticipated)\b", block_text, re.I)),
                    evidence=_evidence(text, "education", block[0].start, block[-1].end),
                )
            )
        return facts

    def _projects(self, text: str, lines: list[Line]) -> list[ProjectFact]:
        blocks: list[list[Line]] = []
        for line in lines:
            starts_project = _header_like(line) and not _TECH_LABEL.match(line.text)
            if not blocks or (starts_project and len(blocks[-1]) > 1):
                blocks.append([line])
            else:
                blocks[-1].append(line)
        facts: list[ProjectFact] = []
        for block in blocks:
            header = block[0]
            name = _clean(re.split(r"\s*\|\s*|\s+[–—-]\s+", _RANGE.sub(" ", header.text))[0])
            name = _clean(_SINGLE_DATE.sub(" ", name).split(":")[0]) or "Untitled project"
            description_lines = [
                _BULLET.sub("", line.text).strip()
                for line in block[1:]
                if not _TECH_LABEL.match(line.text)
            ]
            block_start, block_end = block[0].start, block[-1].end
            technologies: list[str] = []
            for canonical, _, _ in self.vocabulary.find(text[block_start:block_end], body=False):
                if canonical not in technologies:
                    technologies.append(canonical)
            facts.append(
                ProjectFact(
                    name=name[:200],
                    description=" ".join(description_lines)[:2000] or None,
                    technologies=technologies[:40],
                    evidence=_evidence(text, "projects", header.start, header.end),
                )
            )
        return facts


def _pattern(surface: str, *, strict: bool) -> re.Pattern[str]:
    body = re.escape(surface)
    if strict and surface != surface.lower():
        body = f"(?:{body}|{re.escape(surface.upper())})"
        flags = 0
    else:
        flags = re.IGNORECASE
    return re.compile(rf"(?<![A-Za-z0-9+#]){body}(?![A-Za-z0-9+#])", flags)


def _lines(text: str) -> list[Line]:
    lines: list[Line] = []
    offset = 0
    for raw in text.split("\n"):
        stripped = raw.strip()
        if stripped:
            lines.append(Line(text=stripped, start=offset + raw.index(stripped)))
        offset += len(raw) + 1
    return lines


def _sections(lines: list[Line]) -> dict[str, list[Line]]:
    sections: dict[str, list[Line]] = {}
    current: str | None = None
    for line in lines:
        candidate = re.sub(r"\s+", " ", line.text.rstrip(":").strip()).casefold()
        heading = _HEADING_LOOKUP.get(candidate) if len(candidate) <= 40 else None
        if heading is not None:
            current = heading
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return sections


def _section_at(sections: dict[str, list[Line]], position: int) -> str:
    for name, lines in sections.items():
        if any(line.start <= position < line.end for line in lines):
            return name
    return "header"


def _header_like(line: Line) -> bool:
    """Return True for a short, non-bullet line that does not read as a sentence."""
    value = line.text
    reads_as_sentence = value.endswith(".") and len(value.split()) > 5
    return (
        not _BULLET.match(value)
        and len(value) <= 100
        and not reads_as_sentence
        and (value[:1].isupper() or value[:1].isdigit())
    )


def _header_start(lines: list[Line], anchor: int, floor: int) -> int:
    """Walk up to two header-like lines above a dated line, staying inside its entry."""
    start = anchor
    while start - 1 >= floor and anchor - (start - 1) <= 2 and _header_like(lines[start - 1]):
        start -= 1
    return start


def _strip_label(line: Line) -> tuple[str, int]:
    text = _BULLET.sub("", line.text)
    offset = line.start + (len(line.text) - len(text))
    label, sep, rest = text.partition(":")
    if sep and len(label) <= 40 and rest.strip():
        return rest.strip(), offset + len(label) + 1 + (len(rest) - len(rest.lstrip()))
    return text, offset


def _split_items(content: str, offset: int) -> list[tuple[str, int]]:
    items: list[tuple[str, int]] = []
    position = 0
    for piece in _LIST_SPLIT.split(content):
        index = content.find(piece, position)
        if index < 0 or not piece.strip():
            continue
        position = index + len(piece)
        stripped = piece.strip().strip("()").strip()
        if stripped:
            items.append((stripped, offset + index + piece.find(stripped)))
    return items


def _evidence(text: str, section: str, start: int, end: int) -> Evidence:
    end = min(end, start + 2000)
    return Evidence(section=section, text=text[start:end], start=start, end=end)


def _add_skill(
    facts: dict[str, SkillFact],
    name: str,
    canonical: str | None,
    category: str | None,
    source: SkillSource,
    evidence: Evidence,
) -> None:
    key = (canonical or name).casefold()
    existing = facts.get(key)
    if existing is None:
        facts[key] = SkillFact(
            name=name[:100],
            canonical=canonical,
            category=category,
            source=source,
            evidence=[evidence],
        )
    elif len(existing.evidence) < _MAX_EVIDENCE:
        existing.evidence.append(evidence)


def _parse_date(value: str) -> YearMonth | None:
    value = value.strip().rstrip(".")
    numeric = re.fullmatch(r"(\d{1,2})/(\d{4})", value)
    if numeric:
        month = int(numeric.group(1))
        return YearMonth(year=int(numeric.group(2)), month=month if 1 <= month <= 12 else None)
    if re.fullmatch(r"\d{4}", value):
        return YearMonth(year=int(value))
    named = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{4})", value)
    if named:
        return YearMonth(year=int(named.group(2)), month=_MONTHS.get(named.group(1)[:3].casefold()))
    return None


def _split_role(header: str) -> tuple[str | None, str | None, str | None]:
    location_match = _LOCATION.search(header)
    location = location_match.group(0) if location_match else None
    remainder = header
    if location_match:
        remainder = f"{header[: location_match.start()]} | {header[location_match.end() :]}"
    fragments = [_clean(part) for part in _FIELD_SPLIT.split(remainder)]
    fragments = [part for part in fragments if part and not _SINGLE_DATE.fullmatch(part)]
    if len(fragments) == 1 and "," in fragments[0]:
        fragments = [_clean(part) for part in fragments[0].split(",") if _clean(part)]
    title = next((part for part in fragments if _TITLE_WORDS.search(part)), None)
    if title is None and len(fragments) >= 2:
        # No recognizable title word: assume the common "Title, Employer" order.
        return fragments[0], fragments[1], location
    employer = next((part for part in fragments if part != title), None)
    return title, employer, location


def _split_degree(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    abbreviation = _DEGREE_ABBREVIATION.match(value)
    if abbreviation:
        return abbreviation.group(1), abbreviation.group(2).strip()[:200]
    match = re.search(r"\s+(?:in|of)\s+(?!Science\b|Arts\b|Business Administration\b)(.+)$", value)
    if match:
        return value[: match.start()].strip()[:200] or None, match.group(1).strip()[:200]
    return value[:200], None


def _clean(value: str) -> str:
    cleaned = _BULLET.sub("", value)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip(" \t|,;:–—-")


def _first_fragment(text: str, pattern: re.Pattern[str]) -> str:
    for part in re.split(r"\s*(?:\||,|–|—|\t)\s*", _RANGE.sub(" ", text)):
        if pattern.search(part):
            return part
    return text


__all__ = ["PARSER_NAME", "PARSER_VERSION", "ParsedResume", "ResumeParser", "SkillVocabulary"]
