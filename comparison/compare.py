"""Phase 6.3: compare one posting with the approved resume, with evidence for every call.

Four dimensions, each scored 0-1 and explained:

* **Requirements coverage** - each requirement line is met, partly met, or missing,
  quoting the resume text that supports it (skill evidence, experience dates, degree).
* **ATS keyword overlap** - the posting's taxonomy skills found on the resume, each
  weighted by how rare it is across current postings, plus the keywords to add.
* **Seniority fit** - the posting's level and minimum years against the resume.
* **Domain alignment** - industries the posting works in that the resume also shows.

The fit score is their weighted average (weights in ``config/comparison.yaml``),
rescaled over the dimensions that apply to the posting.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from comparison.requirements import (
    OPEN_END,
    OPEN_START,
    Requirement,
    content_text,
    extract_requirements,
)
from comparison.vocabulary import STOPWORDS, TOKEN, RequirementVocabulary, phrase_pattern, stem
from models.candidate import EducationFact, ExperienceFact, ExtractedFacts, SkillFact
from models.job_posting import JobPosting
from profiles.profile_source import experience_years
from utils.config import ComparisonConfig

DEGREE_LEVELS = (
    (5, re.compile(r"ph\.?\s?d|doctor", re.IGNORECASE)),
    (4, re.compile(r"master|m\.s\.|\bms\b|mba", re.IGNORECASE)),
    (3, re.compile(r"bachelor|b\.s\.|b\.a\.|\bbs\b|\bba\b", re.IGNORECASE)),
    (2, re.compile(r"associate|a\.a\.|a\.s\.", re.IGNORECASE)),
)
DEGREE_NAMES = {2: "associate degree", 3: "bachelor's degree", 4: "master's degree", 5: "PhD"}
# "Minimum of 4+ years of experience" around the number in a years line; what is left
# names the work the years must be in.
YEARS_PHRASE = re.compile(
    r"\b(minimum( of)?|at least|over|more than)?\s*\d{1,2}\s*\+?\s*"
    r"((-|–|to)\s*\d{1,2}\s*\+?\s*)?years?('|’)?( of)?( experience)?",
    re.IGNORECASE,
)
_FUNCTION_WORDS = frozenset(
    """with that this have from your their into within such including other others more
    must will able also both each than them they what when where which while been being
    some like about over under across through using""".split()
)


@dataclass(frozen=True)
class ResumeView:
    """What the comparison needs from an approved resume."""

    skills: dict[str, SkillFact]
    years: float
    degree_level: int | None
    degree: EducationFact | None
    education: list[EducationFact]
    text: str
    terms: frozenset[str]
    roles: list[str]
    lines: tuple[str, ...] = ()
    experience: tuple[ExperienceFact, ...] = ()

    @classmethod
    def from_facts(cls, facts: ExtractedFacts, text: str) -> ResumeView:
        """Build the view from approved facts and the resume's extracted text."""
        skills: dict[str, SkillFact] = {}
        for fact in facts.skills:
            for name in (fact.canonical, fact.name):
                if name:
                    skills.setdefault(name.casefold(), fact)
        best: tuple[int, EducationFact] | None = None
        for education in facts.education:
            label = " ".join(part for part in (education.degree, education.field_of_study) if part)
            level = next((value for value, pattern in DEGREE_LEVELS if pattern.search(label)), None)
            if level is not None and (best is None or level > best[0]):
                best = (level, education)
        roles = [
            f"{role.title}{f', {role.employer}' if role.employer else ''}"
            f"{_span(role.start, role.end, role.current)}"
            for role in facts.experience
        ]
        return cls(
            skills=skills,
            years=experience_years(facts.experience),
            degree_level=best[0] if best else None,
            degree=best[1] if best else None,
            education=list(facts.education),
            text=text,
            terms=frozenset(_terms(text)),
            roles=roles,
            lines=tuple(line.strip() for line in text.splitlines() if line.strip()),
            experience=tuple(facts.experience),
        )

    def has_skill(self, skill: str) -> SkillFact | None:
        """Return the resume's fact for ``skill`` (case-insensitive), if any."""
        return self.skills.get(skill.casefold())


_FIELD_CLAUSE = re.compile(r"\bin\s+(?!lieu\b)(?P<fields>[^.;()]+)", re.IGNORECASE)
_GENERIC_FIELD_TERMS = frozenset(
    {"related", "field", "fields", "quantitative", "technical", "discipline", "area", "similar"}
)


def _degree_fields(text: str) -> set[str]:
    """Return the field-of-study terms a degree requirement names, if any."""
    match = _FIELD_CLAUSE.search(text)
    if not match:
        return set()
    return _terms(match["fields"]) - _GENERIC_FIELD_TERMS


def _span(start: object, end: object, current: bool) -> str:
    def label(value: object) -> str:
        return value.label() if hasattr(value, "label") else ""

    if not start:
        return ""
    return f" ({label(start)} - {'present' if current else label(end) or '?'})"


def _terms(text: str) -> set[str]:
    return {stem(token) for token in TOKEN.findall(text.casefold()) if token not in STOPWORDS}


def _technical_degree(
    text: str,
    resume: ResumeView,
    settings: ComparisonConfig,
    vocabulary: RequirementVocabulary,
) -> bool:
    """Whether a line accepting any technical field is met by the resume's degree field."""
    rules = settings.degree_fields
    terms = phrase_pattern(rules.technical_terms)
    match = _FIELD_CLAUSE.search(text)
    if terms is None or not match or not terms.search(match["fields"]):
        return False
    groups = {
        group
        for education in resume.education
        for group in vocabulary.field_groups(vocabulary.fields_in(_education_label(education)))
    }
    return bool(groups & set(rules.technical_groups))


def _month_label(index: int) -> str:
    return f"{index // 12}-{index % 12 + 1:02d}"


def _window_label(first: int, last: int) -> str:
    """Label a graduation window: "2027-12", "2026-06 to 2027-12", "2027-12 or later"."""
    if first == OPEN_START and last == OPEN_END:
        return "any time"
    if first == OPEN_START:
        return f"by {_month_label(last)}"
    if last == OPEN_END:
        return f"{_month_label(first)} or later"
    return (
        _month_label(first) if first == last else f"{_month_label(first)} to {_month_label(last)}"
    )


def _education_label(education: EducationFact) -> str:
    return " ".join(part for part in (education.degree, education.field_of_study) if part)


def _concept_item(
    concept: str, resume: ResumeView, vocabulary: RequirementVocabulary
) -> dict[str, object]:
    """Judge one plain-language concept against the resume, with the line that shows it."""
    item: dict[str, object] = {"concept": concept, "label": vocabulary.label(concept)}
    hit = vocabulary.resume_evidence(concept, resume.lines)
    if hit is not None:
        item.update(met=True, evidence={"term": hit.term, "text": hit.line})
        return item
    for education in resume.education:
        label = _education_label(education)
        groups = vocabulary.field_groups(vocabulary.fields_in(label))
        if groups & vocabulary.concept_fields(concept):
            item.update(
                met=True,
                evidence={"term": "degree", "text": f"{label}, {education.institution or ''}"},
            )
            return item
    item["met"] = False
    return item


def _evidence(fact: SkillFact) -> dict[str, object] | None:
    if not fact.evidence:
        return None
    first = fact.evidence[0]
    return {"section": first.section, "text": first.text, "start": first.start, "end": first.end}


def _status(score: float) -> str:
    if score >= 0.999:
        return "met"
    if score > 0:
        return "partial"
    return "missing"


def years_subject(
    text: str, settings: ComparisonConfig, vocabulary: RequirementVocabulary
) -> set[str]:
    """Return the job a years line asks for experience in, as comparable terms.

    Only lines that name a job (a ``role_patterns`` match) have a subject: "4+ years of
    experience as a Product Manager" -> {"product", "manag"}. "6+ years of experience"
    and "2+ years deploying software to edge systems" -> set(), which keeps the plain
    comparison with total years.
    """
    rules = settings.years_subject
    if not rules.enabled or not any(
        re.search(pattern, text, re.IGNORECASE) for pattern in rules.role_patterns
    ):
        return set()
    generic = {word.casefold() for word in rules.generic_terms}
    rest = YEARS_PHRASE.sub(" ", text)
    return {
        vocabulary.canonical(token)
        for raw in TOKEN.findall(rest.casefold())
        if (token := raw.strip(".-")) and token not in generic and token not in _FUNCTION_WORDS
    }


def _roles_showing(
    subject: set[str],
    resume: ResumeView,
    settings: ComparisonConfig,
    vocabulary: RequirementVocabulary,
) -> list[ExperienceFact]:
    """Return the resume roles whose title or highlights share enough subject terms."""
    needed = min(settings.years_subject.min_shared_terms, len(subject))
    relevant: list[ExperienceFact] = []
    for role in resume.experience:
        text = " ".join([role.title, *role.highlights])
        terms = {vocabulary.canonical(raw.strip(".-")) for raw in TOKEN.findall(text.casefold())}
        if len(subject & terms) >= needed:
            relevant.append(role)
    return relevant


def leadership_roles(
    text: str, resume: ResumeView, settings: ComparisonConfig
) -> list[ExperienceFact] | None:
    """Return the resume roles that show leadership, when ``text`` asks for leadership.

    ``None`` means the line is not a leadership line. "5+ years in a leadership role" or
    "3+ years managing engineering teams" count roles whose title or highlights show
    leading people (``evidence_patterns``); "2+ years managing managers" counts only
    roles with a manager title (``manager_title_patterns``).
    """
    rules = settings.leadership
    if not rules.enabled or not any(
        re.search(pattern, text, re.IGNORECASE) for pattern in rules.line_patterns
    ):
        return None
    managers = any(re.search(p, text, re.IGNORECASE) for p in rules.manager_line_patterns)
    roles: list[ExperienceFact] = []
    for role in resume.experience:
        if managers:
            shown = any(
                re.search(p, role.title, re.IGNORECASE) for p in rules.manager_title_patterns
            )
        else:
            role_text = " ".join([role.title, *role.highlights])
            shown = any(re.search(p, role_text, re.IGNORECASE) for p in rules.evidence_patterns)
        if shown:
            roles.append(role)
    return roles


def leadership_shown(
    text: str, resume: ResumeView, settings: ComparisonConfig
) -> list[ExperienceFact] | None:
    """Return the roles that show leading people, when a line without years asks for it.

    ``None`` means the line does not ask for leadership (``skill_line_patterns``).
    """
    rules = settings.leadership
    if not rules.enabled or not any(
        re.search(pattern, text, re.IGNORECASE) for pattern in rules.skill_line_patterns
    ):
        return None
    return [
        role
        for role in resume.experience
        if any(
            re.search(pattern, " ".join([role.title, *role.highlights]), re.IGNORECASE)
            for pattern in rules.evidence_patterns
        )
    ]


def _leadership_score(have: float, needed: float) -> float:
    """Years in leading roles against the years asked: met, within a year, or missing."""
    if have >= needed:
        return 1.0
    return 0.5 if have > 0 and have >= needed - 1 else 0.0


def _role_label(role: ExperienceFact) -> str:
    return (
        f"{role.title}{f', {role.employer}' if role.employer else ''}"
        f"{_span(role.start, role.end, role.current)}"
    )


def _unrelated_credit(requirement: Requirement, settings: ComparisonConfig) -> float:
    rules = settings.degree_fields
    broad = {name.casefold() for name in rules.broad_fields}
    named = {name.casefold() for name in requirement.fields}
    if not named or named <= broad:
        return rules.broad_credit
    return rules.unrelated_credit


def _line_years_factor(
    requirement: Requirement,
    resume: ResumeView,
    settings: ComparisonConfig,
    vocabulary: RequirementVocabulary,
    result: dict[str, object],
) -> float:
    """Scale a skill or concept line's score by the years it asks for.

    "5+ years of Python": having the skill without all the years is half a match. Far
    short ("10+ years of software development" against three) is no match: below
    ``line_years.missing_below_share`` of the asked years, or no years at all in the job
    the line names ("5+ years as a front-end engineer"), the line is missing. Leadership
    lines count only roles that lead people (``leadership_roles``).
    """
    needed = requirement.min_years
    if not needed:
        return 1.0
    leading = leadership_roles(requirement.text, resume, settings)
    if leading is not None:
        have = experience_years(leading)
        result.update(leadership=True, leadership_roles=[_role_label(r) for r in leading])
        if have < needed:
            result.update(needed_years=needed, resume_years=have)
        return _leadership_score(have, needed)
    subject = years_subject(requirement.text, settings, vocabulary)
    if subject:
        relevant = _roles_showing(subject, resume, settings, vocabulary)
        have = experience_years(list(relevant))
        result["subject"] = sorted(subject)
    else:
        have = resume.years
    if have >= needed:
        return 1.0
    result.update(needed_years=needed, resume_years=have)
    if have <= 0 or have < needed * settings.line_years.missing_below_share:
        return 0.0
    return 0.5


def assess_requirement(
    requirement: Requirement,
    resume: ResumeView,
    settings: ComparisonConfig,
    vocabulary: RequirementVocabulary | None = None,
) -> dict[str, object]:
    """Judge one requirement line against the resume."""
    result: dict[str, object] = {
        "text": requirement.text,
        "required": requirement.required,
        "kind": requirement.kind,
    }
    vocabulary = vocabulary or RequirementVocabulary(settings)
    weight = 1.0 if requirement.required else settings.preferred_weight
    concepts = [_concept_item(name, resume, vocabulary) for name in requirement.concepts]
    concepts_met = sum(bool(item["met"]) for item in concepts)
    if not requirement.min_years and requirement.kind != "degree":
        shown = leadership_shown(requirement.text, resume, settings)
        if shown is not None:
            # "Proven leadership skills" or "lead and motivate a team": any role that shows
            # leading people meets it, quoted as evidence.
            score = 1.0 if shown else 0.0
            result.update(
                leadership=True,
                evidence={"roles": [_role_label(role) for role in shown]},
                score=score,
                status=_status(score),
                weight=round(weight, 3),
            )
            return result
    if requirement.kind == "trait":
        # A resume cannot show curiosity or ownership; the candidate judges these.
        result.update(score=0.0, status="self_assess", weight=0.0)
        return result
    if requirement.kind == "concept":
        score = concepts_met / len(concepts) if concepts else 0.0
        result["concepts"] = concepts
        score *= _line_years_factor(requirement, resume, settings, vocabulary, result)
    elif requirement.kind == "skill":
        matched = [
            {"skill": skill, "evidence": _evidence(fact)}
            for skill in requirement.skills
            if (fact := resume.has_skill(skill)) is not None
        ]
        missing = [skill for skill in requirement.skills if resume.has_skill(skill) is None]
        found = len(matched) + concepts_met
        total = len(requirement.skills) + len(concepts)
        if requirement.any_of:
            score = 1.0 if found else 0.0
        else:
            score = found / total
        result.update(any_of=requirement.any_of, matched=matched, missing=missing)
        if concepts:
            result["concepts"] = concepts
        score *= _line_years_factor(requirement, resume, settings, vocabulary, result)
    elif requirement.kind == "years":
        needed = requirement.min_years or 0.0
        subject = years_subject(requirement.text, settings, vocabulary)
        leading = leadership_roles(requirement.text, resume, settings)
        if leading is not None:
            # "5+ years in a leadership role" counts only roles that show leading people.
            relevant_years = experience_years(leading)
            roles = [_role_label(role) for role in leading]
            result["leadership"] = True
        elif subject:
            # "4+ years as a Product Manager" counts only roles that show that work.
            relevant = _roles_showing(subject, resume, settings, vocabulary)
            relevant_years = experience_years(list(relevant))
            roles = [_role_label(role) for role in relevant]
            result["subject"] = sorted(subject)
        else:
            relevant_years, roles = resume.years, resume.roles
        if leading is not None:
            score = _leadership_score(relevant_years, needed)
        elif relevant_years >= needed:
            score = 1.0
        elif relevant_years >= needed - 1 and (relevant_years > 0 or not subject):
            score = 0.5
        else:
            score = 0.0
        result.update(needed_years=needed, resume_years=relevant_years, evidence={"roles": roles})
    elif requirement.kind == "graduation":
        expected = [
            education
            for education in resume.education
            if education.expected and education.graduation and education.graduation.month
        ]
        if not expected:
            # Without an expected graduation date the resume cannot answer this.
            result.update(score=0.0, status="self_assess", weight=0.0)
            return result
        inside = [
            education
            for education in expected
            if education.graduation
            and education.graduation.month
            and any(
                first <= education.graduation.year * 12 + education.graduation.month - 1 <= last
                for first, last in requirement.graduation_windows
            )
        ]
        score = 1.0 if inside else 0.0
        degree_shown = (inside or expected)[0]
        result.update(
            needed_graduation=[
                _window_label(first, last) for first, last in requirement.graduation_windows
            ],
            evidence={
                "degree": _education_label(degree_shown),
                "graduation": (
                    degree_shown.graduation.label() if degree_shown.graduation else None
                ),
            },
        )
    elif requirement.kind == "degree":
        needed_level = requirement.degree_level
        level = resume.degree_level or 0
        equivalent = requirement.degree_or_equivalent and resume.years >= 4
        degree = resume.degree
        resume_labels = [_education_label(education) for education in resume.education]
        field_match: str
        if requirement.fields:
            # Named fields are compared by group: a data science degree meets "fields such
            # as Computer Science or Mathematics".
            fields = set(requirement.fields)
            resume_fields = {
                name for label in resume_labels for name in vocabulary.fields_in(label)
            }
            if fields & resume_fields:
                field_match = "exact"
            elif vocabulary.field_groups(list(fields)) & vocabulary.field_groups(
                list(resume_fields)
            ):
                field_match = "related"
            else:
                field_match = "none"
        else:
            fields = _degree_fields(requirement.text)
            field_match = (
                "exact" if not fields or fields & _terms(" ".join(resume_labels)) else "none"
            )
        if field_match == "none" and _technical_degree(
            requirement.text, resume, settings, vocabulary
        ):
            # "a technical or scientific field", "a quantitative, technical, or business
            # field": a computing, math, science or engineering degree fits.
            field_match = "related"
        level_ok = bool(resume.education) if needed_level is None else level >= needed_level
        if level_ok:
            # Right level in a field the resume does not show: a specific named field
            # ("Bachelor's in Electrical Engineering" vs a data science degree) earns
            # ``degree_fields.unrelated_credit``; a broad one ("a degree in engineering"),
            # or a field this vocabulary does not know, earns ``broad_credit``.
            score = 1.0 if field_match != "none" else _unrelated_credit(requirement, settings)
        elif equivalent:
            score = 1.0
        else:
            score = 0.0
        result["field_match"] = field_match
        result["needed_fields"] = sorted(fields)
        result.update(
            needed_degree=DEGREE_NAMES.get(needed_level or 0, "a degree or background"),
            evidence=(
                {
                    "degree": " ".join(
                        part for part in (degree.degree, degree.field_of_study) if part
                    ),
                    "institution": degree.institution,
                }
                if degree
                else None
            ),
        )
    else:
        # Shared words after stemming and synonym folding: "Built ETL jobs" on the resume
        # supports "experience building data pipelines".
        wanted = vocabulary.term_index(requirement.text)
        have = vocabulary.resume_index(resume.text)
        shared = [term for term in wanted if term in have]
        support = len(shared) / len(wanted) if wanted else 0.0
        if len(shared) >= 2 and support >= settings.text_match_threshold:
            score = 1.0
        elif shared and support >= settings.text_match_threshold / 2:
            score = 0.5
        else:
            score = 0.0
        weight *= settings.text_requirement_weight
        result.update(
            matched_terms=[
                wanted[term]
                if have[term].casefold() == wanted[term]
                else f"{wanted[term]} ({have[term]})"
                for term in shared
            ],
            missing_terms=[wanted[term] for term in wanted if term not in have],
        )
    if requirement.named and requirement.kind in {"skill", "concept", "text"}:
        score = _apply_named(requirement, resume, settings, score, result)
    result.update(score=round(score, 3), status=_status(score), weight=round(weight, 3))
    return result


def _named_evidence(name: str, resume: ResumeView) -> dict[str, object] | None:
    fact = resume.has_skill(name)
    if fact is not None:
        return _evidence(fact) or {"text": name}
    pattern = phrase_pattern([name])
    for line in resume.lines:
        if pattern is not None and pattern.search(line):
            return {"text": line[:200]}
    return None


def _apply_named(
    requirement: Requirement,
    resume: ResumeView,
    settings: ComparisonConfig,
    score: float,
    result: dict[str, object],
) -> float:
    """Score "A, B, or comparable X": a named product counts fully, a comparable one less.

    Without this, "Foundry, Contour, or comparable analytics platforms" would be met by
    any analytics experience. Comparable experience earns ``comparable_credit``; lines
    that accept any alternative outright ("TypeScript, or another general-purpose
    language", "EKS, GKE, or equivalent") give an alternative the resume shows full credit.
    """
    named = [
        {"name": name, "met": evidence is not None, "evidence": evidence}
        for name in requirement.named
        for evidence in [_named_evidence(name, resume)]
    ]
    years_factor = 0.5 if "needed_years" in result else 1.0
    credit = 1.0 if requirement.alternatives == "equivalent" else settings.comparable_credit
    result.update(named=named, alternatives=requirement.alternatives, comparable_credit=credit)
    if any(item["met"] for item in named):
        return years_factor
    return score * credit


def _named_to_add(
    assessed: list[dict[str, object]], keywords: dict[str, object] | None
) -> list[dict[str, object]]:
    """Return products named in requirements that the resume lacks, for "Keywords to add".

    Taxonomy skills already listed by the keyword overlap are left out, so each keyword
    appears once.
    """
    listed = {
        str(item["skill"]).casefold()
        for item in (keywords or {}).get("to_add", [])  # type: ignore[attr-defined]
    }
    found: dict[str, dict[str, object]] = {}
    for item in assessed:
        for entry in item.get("named", []) or []:  # type: ignore[attr-defined]
            name = str(entry["name"])
            if entry["met"] or name.casefold() in listed or name.casefold() in found:
                continue
            found[name.casefold()] = {"skill": name, "required": bool(item["required"])}
    return sorted(found.values(), key=lambda entry: not entry["required"])


def keyword_overlap(
    posting_skills: list[str],
    resume: ResumeView,
    idf: Callable[[str], float],
    requirement_skills: set[str],
    *,
    prior: float = 0.0,
) -> dict[str, object] | None:
    """Return the IDF-weighted share of the posting's skills on the resume."""
    if not posting_skills:
        return None
    weighted = [(skill, idf(skill)) for skill in posting_skills]
    total = sum(weight for _, weight in weighted)
    matched = [
        {"skill": skill, "weight": round(weight, 3), "evidence": _evidence(fact)}
        for skill, weight in weighted
        if (fact := resume.has_skill(skill)) is not None
    ]
    missing = [
        {"skill": skill, "weight": round(weight, 3), "in_requirements": skill in requirement_skills}
        for skill, weight in weighted
        if resume.has_skill(skill) is None
    ]
    missing.sort(key=lambda item: (not item["in_requirements"], -float(str(item["weight"]))))
    matched_weight = sum(float(str(item["weight"])) for item in matched)
    score = (matched_weight + 0.5 * prior) / (total + prior) if total else 0.0
    return {
        "score": round(score, 3),
        "raw_share": round(matched_weight / total, 3) if total else 0.0,
        "matched": matched,
        "to_add": missing,
    }


def domains_in(
    text: str, vocabulary: dict[str, list[str]], minimum_hits: int
) -> dict[str, list[str]]:
    """Return domains with at least ``minimum_hits`` distinct terms present in ``text``."""
    found: dict[str, list[str]] = {}
    for domain, terms in vocabulary.items():
        hits = [
            term
            for term in terms
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text, re.I)
        ]
        if len(hits) >= minimum_hits:
            found[domain] = hits
    return found


def domain_alignment(
    posting: JobPosting, resume: ResumeView, settings: ComparisonConfig
) -> dict[str, object] | None:
    """Return the share of the posting's domains the resume also shows."""
    posting_domains = domains_in(
        content_text(posting), settings.domains, settings.domain_minimum_hits
    )
    if not posting_domains:
        return None
    resume_domains = domains_in(resume.text, settings.domains, 1)
    shared = sorted(set(posting_domains) & set(resume_domains))
    return {
        "score": round(len(shared) / len(posting_domains), 3),
        "posting_domains": {name: terms[:5] for name, terms in posting_domains.items()},
        "shared": {name: resume_domains[name][:5] for name in shared},
        "not_shown": sorted(set(posting_domains) - set(resume_domains)),
    }


def seniority_fit(
    posting: JobPosting, resume: ResumeView, settings: ComparisonConfig
) -> dict[str, object]:
    """Score the posting's level and minimum years against the resume's experience."""
    level = (posting.position.seniority or "unknown").casefold()
    level_score = settings.seniority_scores.get(
        level, settings.seniority_scores.get("unknown", 0.8)
    )
    needed = posting.experience.min_years
    years_score: float | None = None
    if needed is not None:
        gap = needed - resume.years
        years_score = 1.0 if gap <= 0 else 0.7 if gap <= 1 else 0.4 if gap <= 2 else 0.1
    score = min(level_score, years_score) if years_score is not None else level_score
    return {
        "score": round(score, 3),
        "posting_level": level,
        "posting_min_years": needed,
        "resume_years": resume.years,
        "level_score": level_score,
        "years_score": years_score,
    }


def document_frequencies(postings: Iterable[JobPosting]) -> tuple[Counter[str], int]:
    """Return how many postings name each skill, and the number of postings."""
    counts: Counter[str] = Counter()
    total = 0
    for posting in postings:
        total += 1
        counts.update({*posting.skills.explicit, *posting.skills.inferred})
    return counts, total


def compare_posting(
    posting: JobPosting,
    resume: ResumeView,
    settings: ComparisonConfig,
    *,
    skills_in_text: Callable[[str], list[str]],
    frequencies: Counter[str],
    corpus_size: int,
    vocabulary: RequirementVocabulary | None = None,
) -> dict[str, object]:
    """Return the full comparison for one posting."""
    vocabulary = vocabulary or RequirementVocabulary(settings)
    requirements = extract_requirements(
        posting, skills_in_text, limit=settings.max_requirements, vocabulary=vocabulary
    )
    assessed = [assess_requirement(item, resume, settings, vocabulary) for item in requirements]
    weight_total = sum(float(str(item["weight"])) for item in assessed)
    # A neutral prior (0.5 with ``evidence_prior_weight`` pseudo-weight) keeps one or two
    # assessed lines from producing a 0% or 100% coverage on their own.
    prior = settings.evidence_prior_weight
    coverage = (
        (
            sum(float(str(item["score"])) * float(str(item["weight"])) for item in assessed)
            + 0.5 * prior
        )
        / (weight_total + prior)
        if weight_total
        else None
    )
    # Self-assessed lines (curiosity, ownership) are listed but neither met nor missing.
    checkable = [item for item in assessed if item["status"] != "self_assess"]
    required = [item for item in checkable if item["required"]]

    def idf(skill: str) -> float:
        return math.log((corpus_size + 1) / (frequencies.get(skill, 0) + 1)) + 1.0

    posting_skills = list(dict.fromkeys([*posting.skills.explicit, *posting.skills.inferred]))
    requirement_skills = {skill for item in requirements for skill in item.skills}
    keywords = keyword_overlap(
        posting_skills, resume, idf, requirement_skills, prior=settings.evidence_prior_weight
    )
    domain = domain_alignment(posting, resume, settings)
    seniority = seniority_fit(posting, resume, settings)

    weights = settings.weights
    parts = [
        ("requirements", coverage, weights.requirements),
        ("keywords", keywords["score"] if keywords else None, weights.keywords),
        ("seniority", seniority["score"], weights.seniority),
        ("domain", domain["score"] if domain else None, weights.domain),
    ]
    used = [(value, weight) for _, value, weight in parts if value is not None and weight > 0]
    total_weight = sum(weight for _, weight in used)
    fit: float | None
    note: str | None = None
    if coverage is None and keywords is None:
        # Seniority and domain alone say too little; the posting lists no requirements
        # or skills to compare (for example a "description forthcoming" placeholder).
        fit, band = None, "unknown"
    else:
        fit = 100 * sum(float(str(value)) * weight for value, weight in used) / total_weight
        band = (
            "strong"
            if fit >= settings.strong_threshold
            else "moderate"
            if fit >= settings.moderate_threshold
            else "limited"
        )
        if coverage is None:
            # Without a requirements list the score rests on skill keywords alone, which
            # cannot support a "strong" call.
            note = "No requirements section was found; this score uses skill keywords only."
            if band == "strong":
                band = "moderate"
    return {
        "job_id": posting.metadata.job_id,
        "fit_score": round(fit, 1) if fit is not None else None,
        "band": band,
        "note": note,
        "dimensions": {
            "requirements": {
                "score": round(coverage, 3) if coverage is not None else None,
                "evidence_weight": round(weight_total, 2),
                "required_met": sum(item["status"] == "met" for item in required),
                "required_total": len(required),
                "preferred_met": sum(
                    item["status"] == "met" for item in checkable if not item["required"]
                ),
                "preferred_total": len(checkable) - len(required),
                "self_assess": len(assessed) - len(checkable),
                "items": assessed,
            },
            "keywords": keywords,
            "seniority": seniority,
            "domain": domain,
        },
        "gaps": [str(item["text"]) for item in required if item["status"] == "missing"][:8],
        "named_to_add": _named_to_add(assessed, keywords),
    }


__all__ = [
    "ResumeView",
    "assess_requirement",
    "compare_posting",
    "document_frequencies",
    "domain_alignment",
    "domains_in",
    "keyword_overlap",
    "seniority_fit",
]
