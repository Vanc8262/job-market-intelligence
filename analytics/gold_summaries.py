"""Configuration-driven Silver-to-Gold market summaries."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean, median
from uuid import uuid4

from bayesian.skills import skill_share_report
from models.job_posting import JobPosting, PostingStatus, WorkplaceType
from models.lifecycle import LifecycleEntry, LifecycleState
from storage import GoldStorage, SilverStorage
from utils.config import Config, ResumeProfileConfig
from utils.hashing import stable_json_hash
from utils.logger import log_event

_TOKEN_PATTERN = re.compile(r"[a-z0-9+#.]+")


@dataclass(frozen=True)
class GoldBuildFailure:
    """One Silver record that could not be validated for aggregation."""

    record: str
    error: str

    def summary(self) -> dict[str, str]:
        """Return a JSON-serializable failure description."""
        return {"record": self.record, "error": self.error}


@dataclass(frozen=True)
class SilverToGoldResult:
    """Result of one complete Gold summary build."""

    run_id: str
    generated_at: datetime
    silver_records_found: int
    records_analyzed: int
    excluded_inactive: int
    input_fingerprint: str
    outputs: dict[str, Path]
    failures: list[GoldBuildFailure] = field(default_factory=list)

    @property
    def failed(self) -> int:
        """Return the number of invalid Silver records excluded from Gold."""
        return len(self.failures)

    def summary(self) -> dict[str, object]:
        """Return a JSON-serializable build summary."""
        return {
            "run_id": self.run_id,
            "generated_at": self.generated_at.isoformat(),
            "silver_records_found": self.silver_records_found,
            "records_analyzed": self.records_analyzed,
            "excluded_inactive": self.excluded_inactive,
            "input_fingerprint": self.input_fingerprint,
            "outputs": {name: str(path) for name, path in self.outputs.items()},
            "failed": self.failed,
            "failures": [failure.summary() for failure in self.failures],
        }


class SilverToGoldRunner:
    """Build analytics-ready summaries from validated canonical postings."""

    output_keys = {
        "skills": "skills/summary",
        "titles": "titles/summary",
        "employers": "employers/summary",
        "locations": "locations/summary",
        "salaries": "salaries/summary",
        "resume_relevance": "resume_relevance/summary",
        "current_opportunities": "current_opportunities/mart",
        "manifest": "manifest/latest",
    }

    def __init__(
        self,
        config: Config | None = None,
        *,
        silver_storage: SilverStorage | None = None,
        gold_storage: GoldStorage | None = None,
        resume_profile: ResumeProfileConfig | None = None,
        profile_source: dict[str, object] | None = None,
    ) -> None:
        """Initialize storage and the resume relevance profile (YAML unless one is given)."""
        self.config = config or Config()
        self.silver_storage = silver_storage or SilverStorage(self.config.settings.paths.silver_dir)
        self.gold_storage = gold_storage or GoldStorage(self.config.settings.paths.gold_dir)
        self.profile = resume_profile or self.config.resume_profile
        self.profile_source = profile_source or {"source": "config"}

    def run(
        self,
        *,
        include_inactive: bool = False,
        run_id: str | None = None,
        generated_at: datetime | None = None,
    ) -> SilverToGoldResult:
        """Validate Silver records and replace the current Gold summaries."""
        active_run_id = run_id or uuid4().hex
        built_at = generated_at or datetime.now(UTC)
        records, total_found, failures = self._load_silver()
        analyzed = [
            record for record in records if include_inactive or self._is_market_record(record)
        ]
        excluded_inactive = len(records) - len(analyzed)
        fingerprint = self._input_fingerprint(analyzed)
        context: dict[str, object] = {
            "run_id": active_run_id,
            "generated_at": built_at.isoformat(),
            "input_fingerprint": fingerprint,
            "silver_records_found": total_found,
            "records_analyzed": len(analyzed),
            "excluded_inactive": excluded_inactive,
            "invalid_silver_records": len(failures),
        }

        log_event(
            "INFO",
            "Building Gold summaries from {count} Silver records",
            component="gold_pipeline",
            event="gold_build_started",
            run_id=active_run_id,
            count=len(analyzed),
        )

        relevance_rows = [self._relevance_row(record) for record in analyzed]
        resume_summary = self._resume_summary(relevance_rows, context)
        relevance_by_job = {
            str(row["job_id"]): self._number(row["score"]) for row in relevance_rows
        }
        payloads: dict[str, dict[str, object]] = {
            "skills": self._skills_summary(analyzed, context),
            "titles": self._titles_summary(analyzed, relevance_by_job, context),
            "employers": self._employers_summary(analyzed, relevance_by_job, context),
            "locations": self._locations_summary(analyzed, context),
            "salaries": self._salaries_summary(analyzed, context),
            "resume_relevance": resume_summary,
            "current_opportunities": self._current_opportunities_mart(
                analyzed,
                relevance_rows,
                self._load_lifecycle_by_job(),
                context,
                built_at,
            ),
        }

        outputs: dict[str, Path] = {}
        for name, payload in payloads.items():
            path = self.gold_storage.save(self.output_keys[name], payload)
            if path is None:
                raise RuntimeError(f"Gold storage did not return a path for {name}")
            outputs[name] = path

        manifest = {
            **context,
            "profile_name": self.profile.profile_name,
            "datasets": {
                name: {
                    "key": self.output_keys[name],
                    "path": path.relative_to(self.gold_storage.root).as_posix(),
                }
                for name, path in outputs.items()
            },
            "failures": [failure.summary() for failure in failures],
        }
        manifest_path = self.gold_storage.save(self.output_keys["manifest"], manifest)
        if manifest_path is None:
            raise RuntimeError("Gold storage did not return a manifest path")
        outputs["manifest"] = manifest_path

        log_event(
            "SUCCESS",
            "Gold summaries built: {count} datasets, {failed} invalid records",
            component="gold_pipeline",
            event="gold_build_finished",
            run_id=active_run_id,
            count=len(payloads),
            failed=len(failures),
        )
        return SilverToGoldResult(
            run_id=active_run_id,
            generated_at=built_at,
            silver_records_found=total_found,
            records_analyzed=len(analyzed),
            excluded_inactive=excluded_inactive,
            input_fingerprint=fingerprint,
            outputs=outputs,
            failures=failures,
        )

    def _load_silver(self) -> tuple[list[JobPosting], int, list[GoldBuildFailure]]:
        """Retrieve and validate Silver records from storage."""
        records: list[JobPosting] = []
        failures: list[GoldBuildFailure] = []
        paths = sorted(self.silver_storage.root.rglob("*.json"))
        for path in paths:
            relative = path.relative_to(self.silver_storage.root).as_posix()
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                records.append(JobPosting.model_validate(payload))
            except Exception as exc:
                failures.append(GoldBuildFailure(relative, str(exc)))
        return records, len(paths), failures

    @staticmethod
    def _is_market_record(record: JobPosting) -> bool:
        return record.metadata.is_active and record.metadata.status not in {
            PostingStatus.DUPLICATE,
            PostingStatus.EXPIRED,
            PostingStatus.REMOVED,
        }

    @staticmethod
    def _input_fingerprint(records: list[JobPosting]) -> str:
        identities = sorted(
            (
                {
                    "job_id": record.metadata.job_id,
                    "content_hash": record.metadata.content_hash,
                    "status": record.metadata.status.value,
                }
                for record in records
            ),
            key=lambda item: str(item["job_id"]),
        )
        return stable_json_hash({"records": identities})

    def _skills_summary(
        self, records: list[JobPosting], context: dict[str, object]
    ) -> dict[str, object]:
        total = len(records)
        counts: Counter[str] = Counter()
        explicit_counts: Counter[str] = Counter()
        inferred_counts: Counter[str] = Counter()
        skill_categories: dict[str, set[str]] = defaultdict(set)
        category_postings: Counter[str] = Counter()

        for record in records:
            explicit = set(record.skills.explicit)
            inferred = set(record.skills.inferred) - explicit
            counts.update(explicit | inferred)
            explicit_counts.update(explicit)
            inferred_counts.update(inferred)
            for category, skills in record.skills.categories.items():
                if skills:
                    category_postings[category] += 1
                for skill in skills:
                    skill_categories[skill].add(category)

        items = [
            {
                "skill": skill,
                "posting_count": count,
                "posting_share": self._share(count, total),
                "explicit_count": explicit_counts[skill],
                "inferred_count": inferred_counts[skill],
                "categories": sorted(skill_categories[skill]),
            }
            for skill, count in self._ordered_counts(counts)
        ]
        categories = [
            {
                "category": category,
                "posting_count": count,
                "posting_share": self._share(count, total),
            }
            for category, count in self._ordered_counts(category_postings)
        ]
        postings_with_skills = sum(
            bool(record.skills.explicit or record.skills.inferred) for record in records
        )
        return {
            **context,
            "coverage": {
                "postings_with_skills": postings_with_skills,
                "posting_share": self._share(postings_with_skills, total),
                "distinct_skills": len(counts),
            },
            "skills": items,
            "categories": categories,
            "bayesian": self._bayesian_skill_shares(records),
        }

    def _bayesian_skill_shares(self, records: list[JobPosting]) -> dict[str, object] | None:
        """Phase 8 B1: skill shares by role family with empirical-Bayes shrinkage."""
        bayesian = self.config.bayesian
        if not bayesian.skill_shares.enabled or not records:
            return None
        return skill_share_report(
            (
                (record.position.family, set(record.skills.explicit) | set(record.skills.inferred))
                for record in records
            ),
            bayesian.skill_shares,
            credible_level=bayesian.credible_level,
        )

    def _titles_summary(
        self,
        records: list[JobPosting],
        relevance: dict[str, float],
        context: dict[str, object],
    ) -> dict[str, object]:
        total = len(records)
        counts: Counter[str] = Counter()
        families: Counter[str] = Counter()
        seniorities: Counter[str] = Counter()
        variants: dict[str, set[str]] = defaultdict(set)
        scores: dict[str, list[float]] = defaultdict(list)
        for record in records:
            title = record.position.normalized_title or record.position.title
            counts[title] += 1
            variants[title].add(record.position.title)
            scores[title].append(relevance[record.metadata.job_id])
            families[record.position.family or "Unknown"] += 1
            seniorities[record.position.seniority or "unknown"] += 1
        return {
            **context,
            "distinct_titles": len(counts),
            "titles": [
                {
                    "title": title,
                    "posting_count": count,
                    "posting_share": self._share(count, total),
                    "average_resume_relevance": round(fmean(scores[title]), 2),
                    "raw_variants": sorted(variants[title]),
                }
                for title, count in self._ordered_counts(counts)
            ],
            "families": self._count_rows(families, total, "family"),
            "seniority": self._count_rows(seniorities, total, "level"),
        }

    def _employers_summary(
        self,
        records: list[JobPosting],
        relevance: dict[str, float],
        context: dict[str, object],
    ) -> dict[str, object]:
        total = len(records)
        grouped: dict[str, list[JobPosting]] = defaultdict(list)
        for record in records:
            grouped[record.company.normalized_name or record.company.name].append(record)
        rows: list[dict[str, object]] = []
        for employer, postings in grouped.items():
            salary_count = sum(self._has_salary(record) for record in postings)
            remote_count = sum(
                record.location.workplace_type == WorkplaceType.REMOTE for record in postings
            )
            rows.append(
                {
                    "employer": employer,
                    "posting_count": len(postings),
                    "posting_share": self._share(len(postings), total),
                    "remote_count": remote_count,
                    "salary_posting_count": salary_count,
                    "average_resume_relevance": round(
                        fmean(relevance[record.metadata.job_id] for record in postings), 2
                    ),
                }
            )
        rows.sort(
            key=lambda row: (
                -self._number(row["posting_count"]),
                -self._number(row["average_resume_relevance"]),
                str(row["employer"]).casefold(),
            )
        )
        return {**context, "distinct_employers": len(grouped), "employers": rows}

    def _locations_summary(
        self, records: list[JobPosting], context: dict[str, object]
    ) -> dict[str, object]:
        total = len(records)
        locations: Counter[str] = Counter()
        cities: Counter[str] = Counter()
        regions: Counter[str] = Counter()
        countries: Counter[str] = Counter()
        workplace: Counter[str] = Counter()
        for record in records:
            locations[record.location.raw or "Unknown"] += 1
            cities[record.location.city or "Unknown"] += 1
            regions[record.location.region or "Unknown"] += 1
            countries[record.location.country or "Unknown"] += 1
            workplace[record.location.workplace_type.value] += 1
        return {
            **context,
            "distinct_locations": len(locations),
            "locations": self._count_rows(locations, total, "location"),
            "cities": self._count_rows(cities, total, "city"),
            "regions": self._count_rows(regions, total, "region"),
            "countries": self._count_rows(countries, total, "country"),
            "workplace_types": self._count_rows(workplace, total, "workplace_type"),
        }

    def _salaries_summary(
        self, records: list[JobPosting], context: dict[str, object]
    ) -> dict[str, object]:
        salary_records = [record for record in records if self._has_salary(record)]
        currencies: dict[str, list[JobPosting]] = defaultdict(list)
        employers: dict[str, list[JobPosting]] = defaultdict(list)
        for record in salary_records:
            currencies[record.salary.currency].append(record)
            employers[record.company.normalized_name or record.company.name].append(record)
        return {
            **context,
            "coverage": {
                "postings_with_salary": len(salary_records),
                "posting_share": self._share(len(salary_records), len(records)),
                "postings_without_salary": len(records) - len(salary_records),
            },
            "currencies": [
                {
                    "currency": currency,
                    **self._salary_statistics(postings),
                }
                for currency, postings in sorted(currencies.items())
            ],
            "employers": [
                {
                    "employer": employer,
                    **self._salary_statistics(postings),
                }
                for employer, postings in sorted(
                    employers.items(), key=lambda item: (-len(item[1]), item[0].casefold())
                )
            ],
        }

    def _resume_summary(
        self, rows: list[dict[str, object]], context: dict[str, object]
    ) -> dict[str, object]:
        ordered = sorted(
            rows,
            key=lambda row: (
                -self._number(row["score"]),
                str(row["title"]).casefold(),
                str(row["job_id"]),
            ),
        )
        for rank, row in enumerate(ordered, start=1):
            row["rank"] = rank
        scores = [self._number(row["score"]) for row in ordered]
        return {
            **context,
            "profile_source": self.profile_source,
            "profile": {
                "profile_name": self.profile.profile_name,
                "target_titles": self.profile.target_titles,
                "target_families": self.profile.target_families,
                "target_seniority": self.profile.target_seniority,
                "skills": self.profile.skills,
                "desired_skill_matches": self.profile.desired_skill_matches,
                "home_regions": self.profile.home_regions,
                "home_bases": [base.model_dump() for base in self.profile.home_bases],
                "title_tiers": [tier.model_dump() for tier in self.profile.title_tiers],
                "avoid_title_keywords": self.profile.avoid_title_keywords,
                "preferred_employment_types": self.profile.preferred_employment_types,
                "remote_accessible": self.profile.remote_accessible,
                "weights": self.profile.weights.model_dump(),
                "scoring": self.profile.scoring.model_dump(),
            },
            "score_summary": {
                "minimum": round(min(scores), 2) if scores else None,
                "median": round(float(median(scores)), 2) if scores else None,
                "average": round(fmean(scores), 2) if scores else None,
                "maximum": round(max(scores), 2) if scores else None,
                "strong_matches": sum(row["match_band"] == "strong" for row in ordered),
                "moderate_matches": sum(row["match_band"] == "moderate" for row in ordered),
                "limited_matches": sum(row["match_band"] == "limited" for row in ordered),
            },
            "ranked_postings": ordered,
        }

    def _current_opportunities_mart(
        self,
        records: list[JobPosting],
        relevance_rows: list[dict[str, object]],
        lifecycle_by_job: dict[str, LifecycleEntry],
        context: dict[str, object],
        generated_at: datetime,
    ) -> dict[str, object]:
        """Build one analytics-ready row for each current opportunity."""
        relevance_by_job = {str(row["job_id"]): row for row in relevance_rows}
        rows = [
            self._current_opportunity_row(
                record,
                relevance_by_job[record.metadata.job_id],
                lifecycle_by_job.get(record.metadata.job_id),
                generated_at,
            )
            for record in records
        ]
        rows.sort(
            key=lambda row: (
                self._number(row["rank"]),
                -self._number(row["resume_relevance_score"]),
                str(row["job_id"]),
            )
        )
        tracked = sum(bool(row["lifecycle_tracked"]) for row in rows)
        salary = sum(bool(row["has_salary"]) for row in rows)
        return {
            **context,
            "schema_version": 1,
            "grain": "one row per active nonduplicate canonical job posting",
            "row_count": len(rows),
            "coverage": {
                "lifecycle_tracked": tracked,
                "lifecycle_tracking_share": self._share(tracked, len(rows)),
                "postings_with_salary": salary,
                "salary_share": self._share(salary, len(rows)),
            },
            "opportunities": rows,
        }

    def _current_opportunity_row(
        self,
        record: JobPosting,
        relevance: dict[str, object],
        lifecycle: LifecycleEntry | None,
        generated_at: datetime,
    ) -> dict[str, object]:
        """Flatten canonical, ranking, and lifecycle fields for one opportunity."""
        all_skills = list(dict.fromkeys([*record.skills.explicit, *record.skills.inferred]))
        first_seen = lifecycle.first_seen_at if lifecycle else record.metadata.first_seen_at
        last_seen = lifecycle.last_seen_at if lifecycle else record.metadata.last_seen_at
        last_changed = lifecycle.last_changed_at if lifecycle else record.updated_at
        return {
            "rank": relevance["rank"],
            "job_id": record.metadata.job_id,
            "external_id": record.source.external_id,
            "title": record.position.title,
            "normalized_title": record.position.normalized_title or record.position.title,
            "family": record.position.family,
            "seniority": record.position.seniority,
            "employer": record.company.normalized_name or record.company.name,
            "industry": record.company.industry,
            "location": record.location.raw,
            "city": record.location.city,
            "region": record.location.region,
            "country": record.location.country,
            "workplace_type": record.location.workplace_type.value,
            "employment_type": record.employment_type.value,
            "source": record.source.name,
            "source_type": record.source.source_type.value,
            "url": str(record.source.url) if record.source.url else None,
            "posted_at": (
                record.metadata.posted_at.isoformat() if record.metadata.posted_at else None
            ),
            "expires_at": (
                record.metadata.expires_at.isoformat() if record.metadata.expires_at else None
            ),
            "canonical_status": record.metadata.status.value,
            "first_seen_at": first_seen.isoformat(),
            "last_seen_at": last_seen.isoformat(),
            "last_changed_at": last_changed.isoformat(),
            "age_days": self._elapsed_days(generated_at, first_seen),
            "days_since_last_seen": self._elapsed_days(generated_at, last_seen),
            "lifecycle_tracked": lifecycle is not None,
            "lifecycle_status": lifecycle.status.value if lifecycle else "untracked",
            "lifecycle_target": lifecycle.target_name if lifecycle else None,
            "observation_count": lifecycle.observation_count if lifecycle else None,
            "consecutive_missing_runs": (lifecycle.consecutive_missing_runs if lifecycle else None),
            "resume_relevance_score": relevance["score"],
            "match_band": relevance["match_band"],
            "matched_skills": relevance["matched_skills"],
            "skill_match_details": relevance["skill_match_details"],
            "matched_title_signals": relevance["matched_title_signals"],
            "title_match_type": relevance["title_match_type"],
            "family_match": relevance["family_match"],
            "home_region_match": relevance["home_region_match"],
            "home_base": relevance["home_base"],
            "home_base_miles": relevance["home_base_miles"],
            "title_tier": relevance["title_tier"],
            "avoided_title_keywords": relevance["avoided_title_keywords"],
            "employment_preferred": relevance["employment_preferred"],
            "pre_cap_score": relevance["pre_cap_score"],
            "score_cap_reason": relevance["score_cap_reason"],
            "score_cap": relevance["score_cap"],
            "score_components": relevance["score_components"],
            "skills": all_skills,
            "skill_categories": record.skills.categories,
            "experience_level": record.experience.level.value,
            "minimum_experience_years": record.experience.min_years,
            "maximum_experience_years": record.experience.max_years,
            "minimum_degree": record.education.minimum_degree.value,
            "salary_minimum": record.salary.min_amount,
            "salary_maximum": record.salary.max_amount,
            "salary_midpoint": self._salary_midpoint(record),
            "salary_currency": record.salary.currency,
            "salary_pay_period": record.salary.pay_period.value,
            "has_salary": self._has_salary(record),
        }

    def _load_lifecycle_by_job(self) -> dict[str, LifecycleEntry]:
        """Load the latest lifecycle entry for each canonical job when available."""
        if not self.gold_storage.exists("lifecycle/state"):
            return {}
        state = LifecycleState.model_validate(self.gold_storage.load("lifecycle/state"))
        by_job: dict[str, LifecycleEntry] = {}
        for entry in state.entries.values():
            previous = by_job.get(entry.job_id)
            if previous is None or entry.last_seen_at > previous.last_seen_at:
                by_job[entry.job_id] = entry
        return by_job

    def _relevance_row(self, record: JobPosting) -> dict[str, object]:
        profile = self.profile
        policy = profile.scoring
        profile_skill_keys = {self._comparison(skill): skill for skill in profile.skills}
        explicit_keys = {self._comparison(skill) for skill in record.skills.explicit}
        inferred_keys = {self._comparison(skill) for skill in record.skills.inferred}
        core_skill_keys = {self._comparison(skill) for skill in policy.core_skills}
        skill_match_details: list[dict[str, object]] = []
        for key, skill in profile_skill_keys.items():
            if key in explicit_keys:
                evidence = "explicit"
                evidence_weight = policy.explicit_skill_evidence
            elif key in inferred_keys:
                evidence = "inferred"
                evidence_weight = policy.inferred_skill_evidence
            else:
                continue
            importance = "core" if key in core_skill_keys else "supporting"
            importance_weight = (
                policy.core_skill_weight if importance == "core" else policy.supporting_skill_weight
            )
            skill_match_details.append(
                {
                    "skill": skill,
                    "evidence": evidence,
                    "importance": importance,
                    "weighted_value": round(evidence_weight * importance_weight, 3),
                }
            )
        matched_skills = [str(detail["skill"]) for detail in skill_match_details]
        weighted_skill_matches = sum(
            self._number(detail["weighted_value"]) for detail in skill_match_details
        )
        skill_signal = min(1.0, weighted_skill_matches / profile.desired_skill_matches)

        title = record.position.normalized_title or record.position.title
        title_tokens = self._tokens(title)
        exact_title_matches = [
            target
            for target in profile.target_titles
            if self._comparison(target) == self._comparison(title)
        ]
        phrase_title_matches = [
            target
            for target in profile.target_titles
            if self._contains_contiguous_phrase(record.position.title, target)
        ]
        family = record.position.family or ""
        family_match = any(
            self._comparison(family) == self._comparison(target)
            for target in profile.target_families
        )
        if exact_title_matches:
            title_signal = 1.0
            title_match_type = "exact_title"
            matched_titles = exact_title_matches
        elif family_match:
            title_signal = 1.0
            title_match_type = "family"
            matched_titles = phrase_title_matches
        elif phrase_title_matches:
            title_signal = policy.phrase_title_signal
            title_match_type = "phrase"
            matched_titles = phrase_title_matches
        else:
            overlaps = [
                len(title_tokens & self._tokens(target)) / max(1, len(self._tokens(target)))
                for target in profile.target_titles
            ]
            title_signal = min(policy.fuzzy_title_max_signal, max(overlaps, default=0.0))
            title_match_type = "fuzzy" if title_signal else "none"
            matched_titles = []

        title_tier = next(
            (
                tier
                for tier in profile.title_tiers
                if any(
                    self._contains_contiguous_phrase(record.position.title, keyword)
                    for keyword in tier.keywords
                )
            ),
            None,
        )
        if title_tier is not None:
            title_signal *= title_tier.signal
        avoided_keywords = [
            keyword
            for keyword in profile.avoid_title_keywords
            if self._contains_contiguous_phrase(record.position.title, keyword)
        ]
        if avoided_keywords:
            title_signal = 0.0

        seniority = (record.position.seniority or "unknown").casefold()
        target_seniority = {value.casefold() for value in profile.target_seniority}
        seniority_signal = (
            1.0
            if seniority in target_seniority
            else policy.seniority_signals.get(
                seniority, policy.seniority_signals.get("unknown", 0.0)
            )
        )
        minimum_years_signal: float | None = None
        if record.experience.min_years is not None:
            difference = record.experience.min_years - policy.maximum_preferred_experience_years
            if difference <= 0:
                minimum_years_signal = 1.0
            elif difference <= 2:
                minimum_years_signal = policy.near_experience_signal
            else:
                minimum_years_signal = policy.excessive_experience_signal
        experience_signal = (
            min(seniority_signal, minimum_years_signal)
            if minimum_years_signal is not None
            else seniority_signal
        )

        location_text = " ".join(
            value
            for value in (
                record.location.raw,
                record.location.city,
                record.location.region,
            )
            if value
        )
        home_base, home_base_miles = self._nearest_home_base(location_text)
        home_match = home_base is not None or any(
            self._contains_contiguous_phrase(location_text, region)
            for region in profile.home_regions
        )
        if home_match or (
            profile.remote_accessible and record.location.workplace_type == WorkplaceType.REMOTE
        ):
            location_signal = 1.0
        elif record.location.workplace_type == WorkplaceType.HYBRID:
            location_signal = policy.hybrid_non_home_signal
        elif record.location.workplace_type == WorkplaceType.UNKNOWN:
            location_signal = policy.unknown_location_signal
        elif policy.allow_relocation:
            location_signal = policy.relocation_location_signal
        else:
            location_signal = 0.0

        weights = profile.weights
        total_weight = weights.skills + weights.title + weights.experience + weights.location
        normalized_weights = {
            "skills": weights.skills / total_weight * 100,
            "title": weights.title / total_weight * 100,
            "experience": weights.experience / total_weight * 100,
            "location": weights.location / total_weight * 100,
        }
        signals = {
            "skills": skill_signal,
            "title": title_signal,
            "experience": experience_signal,
            "location": location_signal,
        }
        components: dict[str, dict[str, object]] = {
            name: {
                "signal": round(signal, 4),
                "points": round(normalized_weights[name] * signal, 2),
                "maximum_points": round(normalized_weights[name], 2),
            }
            for name, signal in signals.items()
        }
        components["skills"].update(
            {
                "weighted_matches": round(weighted_skill_matches, 3),
                "desired_weighted_matches": profile.desired_skill_matches,
            }
        )
        components["experience"].update(
            {
                "seniority_signal": seniority_signal,
                "minimum_years_signal": minimum_years_signal,
            }
        )
        pre_cap_score = float(
            round(
                sum(self._number(component["points"]) for component in components.values()),
                2,
            )
        )
        employment_type = record.employment_type.value
        employment_preferred = (
            not profile.preferred_employment_types
            or employment_type == "unknown"
            or employment_type in profile.preferred_employment_types
        )
        caps: list[tuple[float, str]] = []
        if (
            not policy.allow_relocation
            and not home_match
            and record.location.workplace_type == WorkplaceType.ONSITE
        ):
            caps.append((policy.non_home_onsite_score_cap, "non_home_onsite_without_relocation"))
        if avoided_keywords:
            caps.append((policy.avoided_title_score_cap, "avoided_role_type"))
        if not employment_preferred and policy.non_preferred_employment_score_cap is not None:
            caps.append(
                (policy.non_preferred_employment_score_cap, "non_preferred_employment_type")
            )
        score = pre_cap_score
        score_cap: float | None = None
        score_cap_reason: str | None = None
        binding = min(caps, default=None)
        if binding is not None and score > binding[0]:
            score_cap, score_cap_reason = binding
            score = score_cap
        return {
            "rank": 0,
            "job_id": record.metadata.job_id,
            "external_id": record.source.external_id,
            "score": score,
            "pre_cap_score": pre_cap_score,
            "score_cap_reason": score_cap_reason,
            "score_cap": score_cap,
            "match_band": self._match_band(score),
            "title": record.position.title,
            "normalized_title": title,
            "family": record.position.family,
            "seniority": record.position.seniority,
            "employer": record.company.normalized_name or record.company.name,
            "location": record.location.raw,
            "workplace_type": record.location.workplace_type.value,
            "source": record.source.name,
            "url": str(record.source.url) if record.source.url else None,
            "posted_at": (
                record.metadata.posted_at.isoformat() if record.metadata.posted_at else None
            ),
            "matched_skills": matched_skills,
            "skill_match_details": skill_match_details,
            "matched_title_signals": matched_titles,
            "title_match_type": title_match_type,
            "family_match": family_match,
            "home_region_match": home_match,
            "home_base": home_base,
            "home_base_miles": home_base_miles,
            "title_tier": title_tier.name if title_tier else None,
            "avoided_title_keywords": avoided_keywords,
            "employment_preferred": employment_preferred,
            "score_components": components,
        }

    def _nearest_home_base(self, location_text: str) -> tuple[str | None, float | None]:
        """Return the closest configured home base the location names, if any."""
        best: tuple[float, str] | None = None
        for base in self.profile.home_bases:
            if base.region_terms and not any(
                self._contains_contiguous_phrase(location_text, term) for term in base.region_terms
            ):
                continue
            for place, miles in base.places.items():
                if miles <= base.max_miles and self._contains_contiguous_phrase(
                    location_text, place
                ):
                    if best is None or miles < best[0]:
                        best = (miles, base.name)
        return (best[1], best[0]) if best else (None, None)

    @staticmethod
    def _salary_statistics(records: list[JobPosting]) -> dict[str, object]:
        minimums = [
            record.salary.min_amount for record in records if record.salary.min_amount is not None
        ]
        maximums = [
            record.salary.max_amount for record in records if record.salary.max_amount is not None
        ]
        midpoints = [
            SilverToGoldRunner._salary_midpoint(record)
            for record in records
            if SilverToGoldRunner._salary_midpoint(record) is not None
        ]
        numeric_midpoints = [value for value in midpoints if value is not None]
        return {
            "posting_count": len(records),
            "annual_minimum_observed": round(min(minimums), 2) if minimums else None,
            "annual_maximum_observed": round(max(maximums), 2) if maximums else None,
            "median_annual_midpoint": (
                round(float(median(numeric_midpoints)), 2) if numeric_midpoints else None
            ),
            "average_annual_midpoint": (
                round(fmean(numeric_midpoints), 2) if numeric_midpoints else None
            ),
        }

    @staticmethod
    def _salary_midpoint(record: JobPosting) -> float | None:
        minimum = record.salary.min_amount
        maximum = record.salary.max_amount
        if minimum is not None and maximum is not None:
            return (minimum + maximum) / 2
        return minimum if minimum is not None else maximum

    @staticmethod
    def _elapsed_days(later: datetime, earlier: datetime) -> int:
        return max(0, (later - earlier).days)

    @staticmethod
    def _has_salary(record: JobPosting) -> bool:
        return record.salary.min_amount is not None or record.salary.max_amount is not None

    @staticmethod
    def _ordered_counts(counts: Counter[str]) -> list[tuple[str, int]]:
        return sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))

    @classmethod
    def _count_rows(cls, counts: Counter[str], total: int, label: str) -> list[dict[str, object]]:
        return [
            {
                label: value,
                "posting_count": count,
                "posting_share": cls._share(count, total),
            }
            for value, count in cls._ordered_counts(counts)
        ]

    @staticmethod
    def _share(count: int, total: int) -> float:
        return round(count / total, 4) if total else 0.0

    @staticmethod
    def _number(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"Expected numeric analytics value, received {value!r}")
        return float(value)

    @staticmethod
    def _comparison(value: str) -> str:
        return " ".join(_TOKEN_PATTERN.findall(value.casefold()))

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return set(_TOKEN_PATTERN.findall(value.casefold()))

    @classmethod
    def _contains_contiguous_phrase(cls, text: str, phrase: str) -> bool:
        normalized_text = f" {cls._comparison(text)} "
        normalized_phrase = cls._comparison(phrase)
        return bool(normalized_phrase) and f" {normalized_phrase} " in normalized_text

    def _match_band(self, score: float) -> str:
        if score >= self.profile.scoring.strong_match_threshold:
            return "strong"
        if score >= self.profile.scoring.moderate_match_threshold:
            return "moderate"
        return "limited"


__all__ = [
    "GoldBuildFailure",
    "SilverToGoldResult",
    "SilverToGoldRunner",
]
