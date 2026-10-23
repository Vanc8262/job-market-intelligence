"""Compiled vocabularies for plain-language requirements.

Postings often ask for abilities in ordinary words rather than tool names: "comfort with
APIs, data pipelines, and cloud infrastructure", "a background in fields such as Computer
Science or Physics", "you approach problems with curiosity". This module turns the
``concepts``, ``fields_of_study``, and ``traits`` lists in ``config/comparison.yaml`` into
matchers so those lines can be judged on meaning rather than on shared words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property

from utils.config import ComparisonConfig


def phrase_pattern(phrases: list[str]) -> re.Pattern[str] | None:
    """Return one pattern matching any phrase as whole words.

    Phrases of one or two characters ("R", "Go", "ML") must match their case exactly;
    longer ones ignore case. Longer phrases are tried first.
    """
    if not phrases:
        return None
    parts = []
    for phrase in sorted(set(phrases), key=len, reverse=True):
        body = re.escape(phrase).replace(r"\ ", r"[\s-]+")
        bounded = rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9+#])"
        parts.append(bounded if len(phrase) <= 2 else f"(?i:{bounded})")
    return re.compile("|".join(parts))


@dataclass(frozen=True)
class ConceptHit:
    """A resume line that shows a concept."""

    concept: str
    label: str
    term: str
    line: str


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
        self._field_pattern = phrase_pattern(names)
        self._trait_pattern = phrase_pattern(settings.traits)

    def label(self, concept: str) -> str:
        """Return a concept's display label."""
        found = self.settings.concepts.get(concept)
        return found.label if found else concept

    def concepts_in(self, text: str) -> list[str]:
        """Return the concepts a requirement line names, in order of appearance."""
        found: list[tuple[int, str]] = []
        for name, pattern in self._posting.items():
            match = pattern.search(text)
            if match:
                found.append((match.start(), name))
        return [name for _, name in sorted(found)]

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


__all__ = ["ConceptHit", "RequirementVocabulary", "phrase_pattern"]
