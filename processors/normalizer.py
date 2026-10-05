"""Configuration-driven normalization for canonical job postings."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from models.job_posting import (
    JobPosting,
    LocationInfo,
    PayPeriod,
    PositionInfo,
    SalaryInfo,
    SkillInfo,
    WorkplaceType,
)
from utils.config import Config

_SPACE_PATTERN = re.compile(r"\s+")
_SALARY_AMOUNT_PATTERN = re.compile(
    r"(?<!\w)(?:USD|CAD|GBP|EUR|US\$|C\$|\$|\u00a3|\u20ac)?\s*"
    r"(?P<amount>\d+(?:,\d{3})*(?:\.\d+)?)\s*(?P<scale>[kKmM])?"
)


class JobPostingNormalizer:
    """Normalize canonical fields using the project's YAML taxonomies."""

    def __init__(self, config: Config | None = None) -> None:
        """Initialize with the shared configuration manager."""
        self.config = config or Config()

    def normalize(self, posting: JobPosting) -> JobPosting:
        """Return a deeply copied posting with normalized canonical fields."""
        normalized = posting.model_copy(deep=True)
        normalized.position = self._normalize_position(normalized.position)
        normalized.company.normalized_name = self._normalize_company_name(
            normalized.company.name
        )
        normalized.location = self._normalize_location(normalized.location)
        normalized.salary = self._normalize_salary(normalized.salary)
        normalized.skills = self._normalize_skills(normalized)
        return normalized

    def _normalize_position(self, position: PositionInfo) -> PositionInfo:
        result = position.model_copy(deep=True)
        title = self._clean_text(result.title)
        matches: list[tuple[int, str, str]] = []
        for canonical in self.config.titles.canonical_titles:
            for surface in [canonical.title, *canonical.aliases]:
                if self._contains_phrase(title, surface):
                    matches.append((len(surface), canonical.family, canonical.title))
        if matches:
            _, result.family, result.normalized_title = max(matches, key=lambda item: item[0])
        else:
            result.normalized_title = title

        seniority_keywords = self.config.titles.seniority_keywords
        seniority_matches: list[tuple[int, int, str]] = []
        for precedence, (level, keywords) in enumerate(seniority_keywords.items()):
            for keyword in self._strings(keywords):
                if self._contains_phrase(title, keyword):
                    seniority_matches.append((precedence, len(keyword), level))
        if seniority_matches:
            result.seniority = max(seniority_matches, key=lambda item: item[:2])[2]
        return result

    def _normalize_company_name(self, name: str) -> str:
        company_config = self.config.companies.model_dump()
        cleaned = self._clean_text(name)
        comparison = self._comparison_key(cleaned)
        aliases = self._mapping(company_config.get("aliases"))
        for alias, canonical in aliases.items():
            if comparison == self._comparison_key(alias):
                return self._clean_text(str(canonical))

        suffixes = sorted(
            self._strings(company_config.get("legal_suffixes")), key=len, reverse=True
        )
        normalized = cleaned
        changed = True
        while changed:
            changed = False
            for suffix in suffixes:
                pattern = rf"(?:\s*[,.-]?\s*){re.escape(suffix)}[.]?$"
                updated = re.sub(pattern, "", normalized, flags=re.IGNORECASE).strip(" ,.-")
                if updated != normalized:
                    normalized = updated
                    changed = True
                    break
        return normalized or cleaned

    def _normalize_location(self, location: LocationInfo) -> LocationInfo:
        config = self.config.locations.model_dump()
        raw = self._optional_clean_text(location.raw)
        city = self._optional_clean_text(location.city)
        region = self._optional_clean_text(location.region)
        country = self._optional_clean_text(location.country)
        workplace_type = location.workplace_type

        if raw and workplace_type == WorkplaceType.UNKNOWN:
            normalized_raw = raw.casefold()
            hybrid_keywords = self._strings(config.get("hybrid_keywords"))
            remote_keywords = self._strings(config.get("remote_keywords"))
            if any(keyword.casefold() in normalized_raw for keyword in hybrid_keywords):
                workplace_type = WorkplaceType.HYBRID
            elif any(keyword.casefold() in normalized_raw for keyword in remote_keywords):
                workplace_type = WorkplaceType.REMOTE
            else:
                workplace_type = WorkplaceType.ONSITE

        countries = self._configured_countries(config)
        if raw:
            for configured_country in countries:
                if self._contains_phrase(raw, configured_country):
                    country = country or configured_country
                    break

        if raw and (city is None or region is None):
            geographic = self._remove_workplace_terms(raw, config)
            parts = [part.strip() for part in geographic.split(",") if part.strip()]
            if len(parts) >= 2:
                final_part = parts[-1]
                if any(final_part.casefold() == item.casefold() for item in countries):
                    country = country or final_part
                    city = city or parts[0]
                    if len(parts) >= 3:
                        region = region or parts[1]
                else:
                    city = city or parts[0]
                    region = region or parts[1]
            elif len(parts) == 1 and not self._only_workplace_term(parts[0], config):
                if any(parts[0].casefold() == item.casefold() for item in countries):
                    country = country or parts[0]
                else:
                    city = city or parts[0]

        if country is None and (city or region):
            country = self._optional_clean_text(config.get("default_country"))
        return location.model_copy(
            update={
                "raw": raw,
                "city": city,
                "region": region,
                "country": country,
                "workplace_type": workplace_type,
            }
        )

    def _normalize_salary(self, salary: SalaryInfo) -> SalaryInfo:
        salary_config = self.config.salary.model_dump()
        normalization = self._mapping(salary_config.get("normalization"))
        minimum = salary.min_amount
        maximum = salary.max_amount
        currency = salary.currency.upper()
        period = salary.pay_period

        if salary.raw_text:
            amounts = self._salary_amounts(salary.raw_text)
            if minimum is None and amounts:
                minimum = amounts[0]
            if maximum is None and amounts:
                maximum = amounts[1] if len(amounts) > 1 else amounts[0]
            currency = self._currency_from_text(salary.raw_text, currency)
            if period == PayPeriod.UNKNOWN:
                period = self._period_from_text(salary.raw_text, period)

        if period == PayPeriod.UNKNOWN:
            period = self._pay_period(normalization.get("default_period"))

        if minimum is not None or maximum is not None:
            if period == PayPeriod.HOURLY:
                annual_hours = self._positive_float(normalization.get("annual_hours"), 2080.0)
                minimum = minimum * annual_hours if minimum is not None else None
                maximum = maximum * annual_hours if maximum is not None else None
                period = PayPeriod.YEARLY
            elif period == PayPeriod.MONTHLY:
                minimum = minimum * 12 if minimum is not None else None
                maximum = maximum * 12 if maximum is not None else None
                period = PayPeriod.YEARLY

        return SalaryInfo(
            min_amount=minimum,
            max_amount=maximum,
            currency=currency,
            pay_period=period,
            raw_text=salary.raw_text,
        )

    def _normalize_skills(self, posting: JobPosting) -> SkillInfo:
        categories = self.config.skills.categories
        haystack = "\n".join(
            value
            for value in (
                posting.position.title,
                posting.description.clean_text,
                posting.description.raw_text,
            )
            if value
        )
        canonical_by_key = {
            self._comparison_key(skill): skill
            for configured_skills in categories.values()
            for skill in configured_skills
        }

        explicit: list[str] = []
        for skill in posting.skills.explicit:
            cleaned = self._clean_text(skill)
            canonical = canonical_by_key.get(self._comparison_key(cleaned), cleaned)
            self._append_unique(explicit, canonical)

        inferred: list[str] = []
        for configured_skills in categories.values():
            for skill in configured_skills:
                if self._contains_phrase(haystack, skill) and not self._contains_value(
                    explicit, skill
                ):
                    self._append_unique(inferred, skill)
        for skill in posting.skills.inferred:
            cleaned = self._clean_text(skill)
            canonical = canonical_by_key.get(self._comparison_key(cleaned), cleaned)
            if not self._contains_value(explicit, canonical):
                self._append_unique(inferred, canonical)

        categorized: dict[str, list[str]] = {}
        combined = [*explicit, *inferred]
        for category, configured_skills in categories.items():
            values = [skill for skill in configured_skills if self._contains_value(combined, skill)]
            if values:
                categorized[category] = values
        return SkillInfo(explicit=explicit, inferred=inferred, categories=categorized)

    @staticmethod
    def _salary_amounts(value: str) -> list[float]:
        amounts: list[float] = []
        for match in _SALARY_AMOUNT_PATTERN.finditer(value):
            amount = float(match.group("amount").replace(",", ""))
            scale = (match.group("scale") or "").casefold()
            if scale == "k":
                amount *= 1_000
            elif scale == "m":
                amount *= 1_000_000
            amounts.append(amount)
            if len(amounts) == 2:
                break
        return amounts

    @staticmethod
    def _currency_from_text(value: str, fallback: str) -> str:
        normalized = value.upper()
        if "CAD" in normalized or "C$" in normalized:
            return "CAD"
        if "GBP" in normalized or "\u00a3" in value:
            return "GBP"
        if "EUR" in normalized or "\u20ac" in value:
            return "EUR"
        if "USD" in normalized or "$" in value:
            return "USD"
        return fallback

    @staticmethod
    def _period_from_text(value: str, fallback: PayPeriod) -> PayPeriod:
        normalized = value.casefold()
        if any(term in normalized for term in ("hour", "/hr", "hourly")):
            return PayPeriod.HOURLY
        if any(term in normalized for term in ("month", "/mo", "monthly")):
            return PayPeriod.MONTHLY
        if any(term in normalized for term in ("year", "annual", "annually")):
            return PayPeriod.YEARLY
        return fallback

    @staticmethod
    def _pay_period(value: object) -> PayPeriod:
        normalized = str(value or "").casefold()
        return next(
            (period for period in PayPeriod if period.value == normalized),
            PayPeriod.UNKNOWN,
        )

    @staticmethod
    def _positive_float(value: object, fallback: float) -> float:
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
        return fallback

    @classmethod
    def _remove_workplace_terms(cls, value: str, config: Mapping[str, Any]) -> str:
        cleaned = value
        keywords = [
            *cls._strings(config.get("remote_keywords")),
            *cls._strings(config.get("hybrid_keywords")),
        ]
        for keyword in sorted(keywords, key=len, reverse=True):
            cleaned = re.sub(re.escape(keyword), "", cleaned, flags=re.IGNORECASE)
        return cleaned.strip(" ,-()")

    @classmethod
    def _only_workplace_term(cls, value: str, config: Mapping[str, Any]) -> bool:
        return not cls._remove_workplace_terms(value, config)

    @classmethod
    def _configured_countries(cls, config: Mapping[str, Any]) -> list[str]:
        countries: list[str] = []
        default = cls._optional_clean_text(config.get("default_country"))
        if default:
            countries.append(default)
        for values in cls._mapping(config.get("regions")).values():
            for country in cls._strings(values):
                cls._append_unique(countries, country)
        return countries

    @staticmethod
    def _contains_phrase(value: str, phrase: str) -> bool:
        pattern = rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])"
        return re.search(pattern, value, flags=re.IGNORECASE) is not None

    @staticmethod
    def _comparison_key(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()

    @staticmethod
    def _clean_text(value: str) -> str:
        return _SPACE_PATTERN.sub(" ", value).strip()

    @classmethod
    def _optional_clean_text(cls, value: object) -> str | None:
        if value is None:
            return None
        cleaned = cls._clean_text(str(value))
        return cleaned or None

    @staticmethod
    def _mapping(value: object) -> dict[str, Any]:
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _strings(value: object) -> list[str]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @staticmethod
    def _contains_value(values: Sequence[str], expected: str) -> bool:
        normalized = expected.casefold()
        return any(value.casefold() == normalized for value in values)

    @classmethod
    def _append_unique(cls, values: list[str], value: str) -> None:
        if not cls._contains_value(values, value):
            values.append(value)


__all__ = ["JobPostingNormalizer"]
