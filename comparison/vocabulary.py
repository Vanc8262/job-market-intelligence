"""Compiled vocabularies for plain-language requirements.

Postings often ask for abilities in ordinary words rather than tool names: "comfort with
APIs, data pipelines, and cloud infrastructure", "a background in fields such as Computer
Science or Physics", "you approach problems with curiosity". This module turns the
``concepts``, ``fields_of_study``, ``traits``, and ``synonyms`` lists in
``config/comparison.yaml`` into matchers so those lines can be judged on meaning rather
than on exact words.

Words are compared after light stemming ("pipelines", "building", "developer" become
"pipelin", "build", "develop") and synonym folding ("ETL" counts as "pipeline",
"troubleshoot" as "debug"), so a resume that says it differently still matches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property

from utils.config import ComparisonConfig

TOKEN = re.compile(r"[a-z][a-z+#.-]{3,}")
STOPWORDS = frozenset(
    """able about above across after against also among amongst ability abilities and any
    applications approach areas around based being best both build building built can
    closely company complex continuous create cross culture deep deliver delivering design
    designing develop developing development drive driving each effective effectively
    empower ensure environment equivalent etc every excellent experience experienced
    expertise fast field focus from fully functional good great hands help high highly
    ideal including into knowledge large level like make making manage management modern
    more most must need needs new other others ownership part passion passionate plus
    preferred problems product products proficiency proven quality related relevant role
    scale scalable similar skills solid solving some strong such support systems team teams
    technical than that their them they things this through tools understanding using various
    well what when where which while will with within work working world years your data
    information""".split()
)
# Longest suffixes first; a stem keeps at least four letters.
_SUFFIXES = (
    ("izations", "iz"),
    ("ization", "iz"),
    ("ations", "at"),
    ("ation", "at"),
    ("ments", ""),
    ("ment", ""),
    ("abilities", "abl"),
    ("ability", "abl"),
    ("ities", ""),
    ("ings", ""),
    ("ing", ""),
    ("ity", ""),
    ("ies", "y"),
    ("ied", "y"),
    ("ers", ""),
    ("er", ""),
    ("ed", ""),
    ("ly", ""),
    ("al", ""),
    ("ive", ""),
    ("s", ""),
)
_MIN_STEM = 4


def stem(word: str) -> str:
    """Return a light stem: "pipelines" -> "pipelin", "developer" -> "develop".

    Deliberately conservative (two suffix passes, stems of at least four letters), so
    unrelated words rarely collide; synonym groups cover irregular forms.
    """
    token = word.casefold()
    for _ in range(2):
        for suffix, replacement in _SUFFIXES:
            if token.endswith(suffix) and not token.endswith("ss"):
                base = token[: -len(suffix)] + replacement
                if len(base) >= _MIN_STEM:
                    token = base
                    break
        else:
            break
    if token.endswith("e") and len(token) > _MIN_STEM:
        token = token[:-1]
    return token


def phrase_pattern(phrases: list[str], *, plurals: bool = True) -> re.Pattern[str] | None:
    """Return one pattern matching any phrase as whole words.

    Phrases of one or two characters ("R", "Go", "ML") must match their case exactly;
    longer ones ignore case and, with ``plurals``, also match a plural ("API" -> "APIs").
    Longer phrases are tried first.
    """
    if not phrases:
        return None
    parts = []
    for phrase in sorted(set(phrases), key=len, reverse=True):
        body = re.escape(phrase).replace(r"\ ", r"[\s-]+")
        if plurals and len(phrase) > 2 and phrase[-1].isalpha():
            body += "(?:e?s)?"
        bounded = rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9+#])"
        parts.append(bounded if len(phrase) <= 2 else f"(?i:{bounded})")
    return re.compile("|".join(parts))


_STOP_STEMS = frozenset(stem(word) for word in STOPWORDS)
_SHORT_SYNONYM = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2,3}(?![A-Za-z0-9])")


@dataclass(frozen=True)
class ConceptHit:
    """A resume line that shows a concept."""

    concept: str
    label: str
    term: str
    line: str


def _any_of(patterns: list[str]) -> re.Pattern[str] | None:
    """Compile configured regular expressions into one case-insensitive pattern."""
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE) if patterns else None


class RequirementVocabulary:
    """Concept, field-of-study, and trait matchers built from the comparison settings."""

    def __init__(self, settings: ComparisonConfig) -> None:
        """Compile every vocabulary once."""
        self.settings = settings
        self._posting = {
            name: pattern
            for name, concept in settings.concepts.items()
            if (pattern := phrase_pattern(concept.posting)) is not None
        }
        # Resume terms are tried in the order listed, so the strongest evidence
        # ("analysis" before "SQL" for data analysis) is quoted first.
        # A concept that a more specific phrase in the line overrides ("A/B testing" is
        # experimentation, not software testing).
        self._not_with = {
            name: pattern
            for name, concept in settings.concepts.items()
            if (pattern := phrase_pattern(concept.not_with)) is not None
        }
        self._resume = {
            name: [
                pattern
                for term in concept.resume
                if (pattern := phrase_pattern([term])) is not None
            ]
            for name, concept in settings.concepts.items()
        }
        names = [name for members in settings.fields_of_study.values() for name in members]
        self._field_groups: dict[str, set[str]] = {}
        for group, members in settings.fields_of_study.items():
            for name in members:
                self._field_groups.setdefault(name.casefold(), set()).add(group)
        self._field_pattern = phrase_pattern(names, plurals=False)
        self._trait_pattern = phrase_pattern(settings.traits)
        # Every listed word (and its stem) folds to the group's first word.
        self._synonyms: dict[str, str] = {}
        for words in settings.synonyms:
            if not words:
                continue
            head = stem(words[0])
            for word in words:
                self._synonyms.setdefault(word.casefold(), head)
                self._synonyms.setdefault(stem(word), head)
        self._index_cache: dict[int, dict[str, str]] = {}
        boilerplate = settings.boilerplate
        self._boilerplate = _any_of(boilerplate.phrases)
        self._section_break = _any_of(boilerplate.section_breaks)
        self.join_continuations = boilerplate.join_continuations

    def canonical(self, word: str) -> str:
        """Return the comparable form of one word (synonyms folded, then stemmed)."""
        lowered = word.casefold()
        if lowered in self._synonyms:
            return self._synonyms[lowered]
        stemmed = stem(lowered)
        return self._synonyms.get(stemmed, stemmed)

    def term_index(self, text: str) -> dict[str, str]:
        """Map each comparable term in ``text`` to the first word that produced it."""
        index: dict[str, str] = {}
        for token in TOKEN.findall(text.casefold()):
            token = token.strip(".-")
            if len(token) < _MIN_STEM or token in STOPWORDS or stem(token) in _STOP_STEMS:
                continue
            index.setdefault(self.canonical(token), token)
        for word in _SHORT_SYNONYM.findall(text):
            # Short synonym-listed words ("ETL", "AWS", "QA") are below the token length.
            if word.casefold() in self._synonyms:
                index.setdefault(self._synonyms[word.casefold()], word)
        return index

    def resume_index(self, text: str) -> dict[str, str]:
        """Return ``term_index`` for the resume text, computed once per resume."""
        key = hash(text)
        if key not in self._index_cache:
            self._index_cache[key] = self.term_index(text)
        return self._index_cache[key]

    def label(self, concept: str) -> str:
        """Return a concept's display label."""
        found = self.settings.concepts.get(concept)
        return found.label if found else concept

    def concepts_in(self, text: str) -> list[str]:
        """Return the concepts a requirement line names, in order of appearance."""
        found: list[tuple[int, str]] = []
        for name, pattern in self._posting.items():
            match = pattern.search(text)
            excluded = self._not_with.get(name)
            if match and not (excluded and excluded.search(text)):
                found.append((match.start(), name))
        return [name for _, name in sorted(found)]

    def is_boilerplate(self, text: str) -> bool:
        """Return whether a line matches a configured never-a-requirement phrase."""
        return bool(self._boilerplate and self._boilerplate.search(text))

    def is_section_break(self, text: str) -> bool:
        """Return whether a line ends the requirements section it appears in."""
        return bool(self._section_break and self._section_break.search(text))

    def is_trait(self, text: str) -> bool:
        """Return whether a line describes disposition rather than experience."""
        return bool(self._trait_pattern and self._trait_pattern.search(text))

    def fields_in(self, text: str) -> list[str]:
        """Return the fields of study named in ``text`` (longest names first)."""
        if self._field_pattern is None:
            return []
        return list(
            dict.fromkeys(match.group(0).casefold() for match in self._field_pattern.finditer(text))
        )

    def field_groups(self, fields: list[str]) -> set[str]:
        """Return the related-field groups the given fields belong to."""
        groups: set[str] = set()
        for name in fields:
            groups |= self._field_groups.get(" ".join(name.casefold().split()), set())
        return groups

    def concept_fields(self, concept: str) -> set[str]:
        """Return the field groups whose degree shows ``concept``."""
        found = self.settings.concepts.get(concept)
        return set(found.fields) if found else set()

    def resume_evidence(self, concept: str, resume_lines: tuple[str, ...]) -> ConceptHit | None:
        """Return the first resume line that shows ``concept``, if any."""
        for pattern in self._resume.get(concept, []):
            for line in resume_lines:
                match = pattern.search(line)
                if match:
                    return ConceptHit(concept, self.label(concept), match.group(0), line[:200])
        return None

    @cached_property
    def has_concepts(self) -> bool:
        """Return whether any concepts are configured."""
        return bool(self._posting)


__all__ = ["STOPWORDS", "TOKEN", "ConceptHit", "RequirementVocabulary", "phrase_pattern", "stem"]
