"""Split a job posting into the requirements a candidate is judged against.

Postings arrive as plain text with headings ("What you'll need", "Bonus") on their own
lines, or, for Lever, with requirement bullets already split out. Each requirement line
is tagged required or preferred, and classified by what it asks for:

* ``skill`` - names taxonomy skills ("Python or Go", "AWS, Kubernetes"),
* ``years`` - a minimum number of years of experience,
* ``degree`` - a minimum degree, or a field of study ("fields such as Computer Science"),
* ``concept`` - broad abilities in plain words ("APIs, data pipelines, cloud infrastructure"),
* ``trait`` - disposition a resume cannot show ("You approach problems with curiosity"),
* ``text`` - anything else, judged by shared words.

Benefits, legal notices, and company blurbs are never treated as requirements.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from comparison.vocabulary import RequirementVocabulary
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
    r"(don't|do not) meet every\b|if you don'?t meet\b|we('|’)re\b|we are\b|today,|"
    r"communication from\b|if you require\b)|\bapply anyway\b|walks of life|"
    r"not everyone (will )?meets?\b|"
    r"accommodation|prohibits?\b|equal (employment )?opportunit|discriminat|"
    r"vacation|paid (time|days) off|401\(k\)|@[\w-]+\.\w",
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
# A line asking for a field of study without saying "degree".
FIELD_CUE = re.compile(
    r"\b(fields? (such as|like|of study)|disciplines? (such as|like)|background\b|"
    r"major(ing)? in|studying|coursework in|related field)",
    re.IGNORECASE,
)
COMPARABLE = re.compile(
    r"\bor (an?other|any other|other|comparable|similar|equivalent|alternative|related)\b|"
    r"\bor the like\b|\bor something similar\b|\band/or similar\b",
    re.IGNORECASE,
)
# "TypeScript, or another general-purpose language" / "or equivalent" accept any
# alternative outright; "or comparable/similar" ask for the named product first.
EQUIVALENT = re.compile(r"\bor (an?other|any other|other|equivalent|alternative)\b", re.I)
# The list of names ends the clause that introduces it ("experience with A, B, or ...").
_LIST_INTRO = re.compile(r"\b(with|in|using|of|like|such as|e\.g\.,?|including)\b|:", re.I)
_LEAD_IN = re.compile(
    r"^(preferably|ideally|especially|particularly|e\.g\.,?|i\.e\.,?|like|such as|"
    r"including|at least one of|one of|either)\s+",
    re.IGNORECASE,
)
# Words that open a sentence rather than name a product.
_NOT_NAMES = frozenset(
    """experience strong proven deep solid demonstrated hands-on knowledge familiarity
    proficiency expertise exposure comfort understanding ability background fluency
    you we our the a an this current modern advanced working""".split()
)
# Capitalized list items that name a business area, not a product ("Recruiting, HR,
# People, or other operational data").
_AREAS = frozenset(
    """people hr recruiting talent sales marketing finance accounting operations legal
    security engineering product design support customers customer success revenue
    procurement payroll benefits compliance risk research data analytics it""".split()
)
_LIST_SPLIT = re.compile(r",|/|\bor\b|\band\b|\(|\)", re.IGNORECASE)
ANY_OF = re.compile(r"\bor\b|/|\bone of\b|\bsuch as\b|\be\.g\.|\bsimilar\b|\bequivalent\b", re.I)
# Perk lines often start with an emoji ("🏝 Generous vacation policy").
_DECORATED = re.compile(r"[^\w\s\"'(“‘]")
_BULLET = re.compile(r"^\s*(?:[-•·*▪◦–]|\d+[.)])\s*")
# A soft-wrapped line continues the one before it: it starts in lowercase, and the line
# before it did not end a sentence or introduce a list.
CONTINUATION = re.compile(r"^[a-z]")
SENTENCE_END = re.compile(r"[.!?;:]\s*$")
_MAX_HEADING_WORDS = 9


@dataclass
class Requirement:
    """One requirement line from a posting."""

    text: str
    required: bool
    kind: str  # "skill" | "years" | "degree" | "concept" | "trait" | "text"
    skills: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    # Products a line names before "or comparable ..." ("Foundry, Contour, or comparable
    # platforms"); having one of them counts more than having something comparable.
    named: list[str] = field(default_factory=list)
    # "equivalent": another product the resume shows counts fully; "comparable": less.
    alternatives: str = ""
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
    your youll haves nice-to-haves we're were we’re you're you’re youre an if be like
    sounds these may might also great ideal who's""".split()
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


def requirement_lines(
    posting: JobPosting, vocabulary: RequirementVocabulary | None = None
) -> list[tuple[str, bool]]:
    """Return (line, required) pairs for the posting's requirement sections.

    ``vocabulary`` adds the configured boilerplate rules (``boilerplate`` in
    ``config/comparison.yaml``): phrases that are never requirements, lines that end a
    requirements section, and joining soft-wrapped continuation lines.
    """
    description = posting.description

    def boilerplate(item: str) -> bool:
        return vocabulary is not None and vocabulary.is_boilerplate(item)

    if description.qualifications:
        return [
            (item, not PREFERRED_ITEM.search(item))
            for item in description.qualifications
            if not boilerplate(item)
        ]
    text = description.clean_text or description.raw_text or ""
    lines = _lines(text)
    found: list[tuple[str, bool]] = []
    section: str | None = None  # None, "required", "preferred"
    join = vocabulary is not None and vocabulary.join_continuations
    # A line that starts in lowercase right after a line that stopped mid-sentence is the
    # rest of that line, wrapped by the source ("with a desire to go" / "above and
    # beyond"). It joins a kept line and is dropped with a skipped one, so the tail of a
    # filtered accommodation notice ("as far in advance as possible.") never stands alone.
    open_sentence = False
    kept = False
    for line in lines:
        continuation = join and open_sentence and CONTINUATION.match(line) is not None
        open_sentence = not SENTENCE_END.search(line)
        if continuation:
            if kept and section is not None:
                merged = f"{found[-1][0]} {line}"
                if NOT_REQUIREMENTS.search(merged) or boilerplate(merged):
                    found.pop()
                    kept = False
                else:
                    required = section == "required" and not PREFERRED_ITEM.search(merged)
                    found[-1] = (merged, required)
            continue
        kept = False
        if vocabulary is not None and vocabulary.is_section_break(line):
            section = None
            continue
        if _is_heading(line):
            open_sentence = False
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
        item = _BULLET.sub("", line).strip().lstrip(":;,. ")
        if (
            len(item.split()) < 3
            or NOT_REQUIREMENTS.search(item)
            or _DECORATED.match(item)
            or boilerplate(item)
        ):
            continue
        found.append((item, section == "required" and not PREFERRED_ITEM.search(item)))
        kept = True
    if found:
        return found
    # No recognizable sections (some sources flatten the text): fall back to sentences
    # that read like requirements.
    sentences = re.split(r"(?<=[.;!?])\s+|\n", text)
    return [
        (sentence.strip(), not PREFERRED_ITEM.search(sentence))
        for sentence in sentences
        if 4 <= len(sentence.split()) <= 60
        and REQUIREMENT_CUES.search(sentence)
        and not NOT_REQUIREMENTS.search(sentence.strip())
        and not boilerplate(sentence)
    ]


def named_items(
    text: str,
    skills_in_text: Callable[[str], list[str]],
    vocabulary: RequirementVocabulary | None,
) -> list[str]:
    """Return the specific products a line names before "or comparable/similar/other"."""
    comparable = COMPARABLE.search(text)
    if comparable is None:
        return []
    head = text[: comparable.start()]
    intros = list(_LIST_INTRO.finditer(head))
    segment = head[intros[-1].end() :] if intros else head
    names: list[str] = []
    for raw in _LIST_SPLIT.split(segment):
        item = _LEAD_IN.sub("", raw.strip(" .;:-'\"")).strip()
        if not item or len(item.split()) > 4 or item.split()[0].casefold() in _NOT_NAMES:
            continue
        if item.casefold() in _AREAS:
            continue
        # Generic abilities ("APIs", "cloud") are concepts, not products.
        if vocabulary and (vocabulary.concepts_in(item) or vocabulary.fields_in(item)):
            continue
        if item[0].isupper() or (len(item.split()) <= 2 and skills_in_text(item)):
            names.append(item)
    return list(dict.fromkeys(names))


def classify(
    text: str,
    required: bool,
    skills_in_text: Callable[[str], list[str]],
    vocabulary: RequirementVocabulary | None = None,
) -> Requirement:
    """Decide what a requirement line asks for."""
    years = YEARS.search(text)
    degree_level = next((level for level, pattern in DEGREES if pattern.search(text)), None)
    if degree_level is None and BARE_DEGREE.search(text):
        degree_level = 3
    fields = vocabulary.fields_in(text) if vocabulary else []
    field_line = bool(fields) and bool(FIELD_CUE.search(text))
    # In a degree line, "in Computer Science, Statistics, ..." names fields of study, not
    # skills: look for skills and concepts only outside that clause.
    degree_like = degree_level is not None or field_line
    skill_text = re.sub(r"\bin\s+[^.;()]+", " ", text) if degree_like else text
    skills = skills_in_text(skill_text)
    concepts = vocabulary.concepts_in(skill_text) if vocabulary and not degree_like else []
    named = [] if degree_like else named_items(text, skills_in_text, vocabulary)
    comparable = COMPARABLE.search(text) if named else None
    alternatives = (
        ""
        if comparable is None
        else "equivalent"
        if EQUIVALENT.match(comparable.group(0))
        else "comparable"
    )
    if degree_like and not skills and not years:
        return Requirement(
            text=text,
            required=required,
            kind="degree",
            degree_level=degree_level,
            fields=fields,
            degree_or_equivalent=bool(re.search(r"equivalent|or related experience", text, re.I)),
        )
    if years and not skills and not concepts:
        return Requirement(
            text=text, required=required, kind="years", min_years=float(years["low"])
        )
    if skills:
        return Requirement(
            text=text,
            required=required,
            kind="skill",
            skills=skills,
            concepts=concepts,
            named=named,
            alternatives=alternatives,
            any_of=len(skills) + len(concepts) > 1 and bool(ANY_OF.search(text)),
            min_years=float(years["low"]) if years else None,
        )
    if concepts:
        return Requirement(
            text=text,
            required=required,
            kind="concept",
            concepts=concepts,
            named=named,
            alternatives=alternatives,
            min_years=float(years["low"]) if years else None,
        )
    if vocabulary and vocabulary.is_trait(text):
        return Requirement(text=text, required=required, kind="trait")
    return Requirement(
        text=text, required=required, kind="text", named=named, alternatives=alternatives
    )


def extract_requirements(
    posting: JobPosting,
    skills_in_text: Callable[[str], list[str]],
    *,
    limit: int = 40,
    vocabulary: RequirementVocabulary | None = None,
) -> list[Requirement]:
    """Return the posting's classified requirements (at most ``limit``)."""
    seen: set[str] = set()
    requirements: list[Requirement] = []
    for text, required in requirement_lines(posting, vocabulary):
        key = " ".join(text.casefold().split())
        if key in seen:
            continue
        seen.add(key)
        requirements.append(classify(text[:500], required, skills_in_text, vocabulary))
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
    "named_items",
    "requirement_lines",
]
