"""Split a job posting into the requirements a candidate is judged against.

Postings arrive as plain text with headings ("What you'll need", "Bonus") on their own
lines, or, for Lever, with requirement bullets already split out. Each requirement line
is tagged required or preferred, and classified by what it asks for:

* ``skill`` - names taxonomy skills ("Python or Go", "AWS, Kubernetes"),
* ``years`` - a minimum number of years of experience,
* ``degree`` - a minimum degree,
* ``text`` - anything else ("Experience building backend systems").

Benefits, legal notices, and company blurbs are never treated as requirements.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from models.job_posting import JobPosting

REQUIRED_HEADINGS = re.compile(
    r"requir|qualif|what you('|’)?ll (need|bring)|what you need|what you bring|you have|"
    r"you bring|to thrive|who you are|about you|looking for|must have|skills|"
    r"what we require|what makes you|your background|you should have|you will need|"
    r"technologies we use|experience",
    re.IGNORECASE,
)
PREFERRED_HEADINGS = re.compile(
    r"nice to have|nice-to-have|bonus|preferred|plus|what we value|extra credit|ideal",
    re.IGNORECASE,
)
# Headings that end a requirements section without starting a new one.
OTHER_HEADINGS = re.compile(
    r"benefit|perk|what we offer|compensation|salary|pay range|equal opportunity|"
    r"privacy|about (us|the company|the team)|who we are|our (mission|values|culture)|"
    r"what you('|’)?ll do|responsibilit|the role|day to day|in this role|location|"
    r"where you('|’)?ll be|how to apply|notice|disclaimer|accommodation",
    re.IGNORECASE,
)
DUTY_HEADINGS = re.compile(
    r"what you('|’)?ll do|what you will do|responsibilit|day to day|day-to-day|"
    r"you can expect to|the role|about (us|the)",
    re.IGNORECASE,
)
PREFERRED_ITEM = re.compile(
    r"\b(a plus|is a plus|are a plus|preferred|nice to have|bonus|ideally|desirable)\b|"
    r"^prefer(red)?\b",
    re.IGNORECASE,
)
# Lines inside a requirements section that are not requirements (diversity statements,
# interview instructions).
NOT_REQUIREMENTS = re.compile(
    r"^(please\b|at [\w&.' -]+, we\b|we (know|encourage|welcome|are committed)\b|"
    r"(don't|do not) meet every\b|if you don'?t meet\b)|\bapply anyway\b",
    re.IGNORECASE,
)
REQUIREMENT_CUES = re.compile(
    r"\b(\d+\+?\s*(?:-\s*\d+\s*)?years?|experience (with|in|building|designing)|"
    r"proficien|knowledge of|familiar|degree|bachelor|master|ph\.?d|ability to|"
    r"skills in|expertise in|understanding of|track record)\b",
    re.IGNORECASE,
)
YEARS = re.compile(
    r"(?P<low>\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*(?P<high>\d{1,2})\s*)?\+?\s*years?",
    re.IGNORECASE,
)
DEGREES = (
    (5, re.compile(r"(?<![A-Za-z])(ph\.?\s?d|doctorate|doctoral)(?![A-Za-z])", re.I)),
    (4, re.compile(r"(?<![A-Za-z])(master'?s?|m\.s\.?|ms degree|mba)(?![A-Za-z])", re.I)),
    (
        3,
        re.compile(
            r"(?<![A-Za-z])(bachelor'?s?|b\.s\.?|b\.a\.?|bs degree|undergraduate degree)"
            r"(?![A-Za-z])",
            re.I,
        ),
    ),
    (2, re.compile(r"(?<![A-Za-z])(associate'?s? degree)(?![A-Za-z])", re.I)),
)
# "degree" alone only counts as an education requirement when it reads like one
# ("a degree in Statistics", "relevant degree"), not "the program supports degree completion".
BARE_DEGREE = re.compile(
    r"\bdegree (in|or|required|preferred)\b|"
    r"\b(a|an|relevant|related|technical|quantitative|four-year|4-year|college|university)"
    r" degree\b",
    re.IGNORECASE,
)
ANY_OF = re.compile(r"\bor\b|/|\bone of\b|\bsuch as\b|\be\.g\.|\bsimilar\b|\bequivalent\b", re.I)
_BULLET = re.compile(r"^\s*(?:[-•·*▪◦–]|\d+[.)])\s*")
_MAX_HEADING_WORDS = 9


@dataclass
class Requirement:
    """One requirement line from a posting."""

    text: str
    required: bool
    kind: str  # "skill" | "years" | "degree" | "text"
    skills: list[str] = field(default_factory=list)
    any_of: bool = False
    min_years: float | None = None
    degree_level: int | None = None
    degree_or_equivalent: bool = False


# Headings written as phrases rather than titles ("To thrive in this role, you have").
HEADING_ENDINGS = re.compile(
    r"(what you('|’)?ll (need|bring|do)|what you need|you have|you bring|who you are|"
    r"about you|requirements|qualifications|nice to have|bonus( points)?|"
    r"preferred( qualifications)?|benefits|perks|what we offer|the role|responsibilities|"
    r"what we value|what we require|you will|you('|’)ll)\s*:?$",
    re.IGNORECASE,
)


HEADING_WORDS = frozenset(
    """a about additional and apply are at background basic benefits bonus bring can
    candidate compensation company culture day description do does education expect
    experience for from have how ideal in information is it job join key life location
    looking makes minimum mission need nice offer of on opportunity or our overview pay
    perks plus points position preferred profile qualifications range requirements
    required responsibilities role salary should skills success summary team the this
    thrive to us value values we what where who why will with work you you'll you’ll
    your youll we're were we’re you're you’re youre an if be like sounds this these
    may might also great ideal who's""".split()
)


def _is_heading(line: str) -> bool:
    words = line.rstrip(":").split()
    if not words or len(words) > _MAX_HEADING_WORDS:
        return False
    if line.endswith(":"):
        return True
    if line.endswith((".", ",", ";")) or _BULLET.match(line):
        return False
    if HEADING_ENDINGS.search(line):
        return True
    if any(char.isdigit() for char in line):
        return False
    tokens = [re.sub(r"[^a-z’']", "", word.casefold()) for word in words]
    tokens = [token for token in tokens if token]
    if not tokens:
        return False
    # A short line counts as a heading when every word is ordinary heading vocabulary
    # ("What You'll Do", "About the Role", "Skills and Experience"); requirement lines
    # name things ("Experience with Snowflake", "Strong Python and SQL skills").
    if len(tokens) <= 7 and all(token in HEADING_WORDS for token in tokens):
        return True
    return line.isupper() and len(tokens) >= 2


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def requirement_lines(posting: JobPosting) -> list[tuple[str, bool]]:
    """Return (line, required) pairs for the posting's requirement sections."""
    description = posting.description
    if description.qualifications:
        return [(item, not PREFERRED_ITEM.search(item)) for item in description.qualifications]
    text = description.clean_text or description.raw_text or ""
    lines = _lines(text)
    found: list[tuple[str, bool]] = []
    section: str | None = None  # None, "required", "preferred"
    for line in lines:
        if _is_heading(line):
            if DUTY_HEADINGS.search(line) or EXCLUDED_SECTIONS.search(line):
                section = None
            elif PREFERRED_HEADINGS.search(line):
                section = "preferred"
            elif REQUIRED_HEADINGS.search(line):
                section = "required"
            else:
                section = None
            continue
        if section is None:
            continue
        item = _BULLET.sub("", line).strip()
        if len(item.split()) < 3 or NOT_REQUIREMENTS.search(item):
            continue
        found.append((item, section == "required" and not PREFERRED_ITEM.search(item)))
    if found:
        return found
    # No recognizable sections (some sources flatten the text): fall back to sentences
    # that read like requirements.
    sentences = re.split(r"(?<=[.;!?])\s+|\n", text)
    return [
        (sentence.strip(), not PREFERRED_ITEM.search(sentence))
        for sentence in sentences
        if 4 <= len(sentence.split()) <= 60 and REQUIREMENT_CUES.search(sentence)
    ]


def classify(text: str, required: bool, skills_in_text: Callable[[str], list[str]]) -> Requirement:
    """Decide what a requirement line asks for."""
    years = YEARS.search(text)
    degree_level = next((level for level, pattern in DEGREES if pattern.search(text)), None)
    if degree_level is None and BARE_DEGREE.search(text):
        degree_level = 3
    # In a degree line, "in Computer Science, Statistics, ..." names fields of study, not
    # skills: look for skills only outside that clause.
    skill_text = re.sub(r"\bin\s+[^.;()]+", " ", text) if degree_level is not None else text
    skills = skills_in_text(skill_text)
    if degree_level is not None and not skills and not years:
        return Requirement(
            text=text,
            required=required,
            kind="degree",
            degree_level=degree_level,
            degree_or_equivalent=bool(re.search(r"equivalent|or related experience", text, re.I)),
        )
    if years and not skills:
        return Requirement(
            text=text, required=required, kind="years", min_years=float(years["low"])
        )
    if skills:
        return Requirement(
            text=text,
            required=required,
            kind="skill",
            skills=skills,
            any_of=len(skills) > 1 and bool(ANY_OF.search(text)),
            min_years=float(years["low"]) if years else None,
        )
    return Requirement(text=text, required=required, kind="text")


def extract_requirements(
    posting: JobPosting, skills_in_text: Callable[[str], list[str]], *, limit: int = 40
) -> list[Requirement]:
    """Return the posting's classified requirements (at most ``limit``)."""
    seen: set[str] = set()
    requirements: list[Requirement] = []
    for text, required in requirement_lines(posting):
        key = " ".join(text.casefold().split())
        if key in seen:
            continue
        seen.add(key)
        requirements.append(classify(text[:500], required, skills_in_text))
        if len(requirements) >= limit:
            break
    return requirements


EXCLUDED_SECTIONS = re.compile(
    r"benefit|perk|what we offer|compensation|salary|pay range|equal opportunity|privacy|"
    r"accommodation|notice|disclaimer|scam|fraud warning|how to apply|insurance",
    re.IGNORECASE,
)


def content_text(posting: JobPosting) -> str:
    """Return the posting's text without benefits, legal, and application sections.

    Benefits sections mention "medical" and "insurance" in nearly every posting, which
    would otherwise make every employer look like a healthcare or finance company.
    """
    text = posting.description.clean_text or posting.description.raw_text or ""
    kept: list[str] = []
    excluded = False
    for line in _lines(text):
        if _is_heading(line):
            excluded = bool(EXCLUDED_SECTIONS.search(line))
            continue
        if not excluded:
            kept.append(line)
    return "\n".join([posting.position.title, *kept])


__all__ = [
    "Requirement",
    "classify",
    "content_text",
    "extract_requirements",
    "requirement_lines",
]
