"""Blocking data-quality gateway for canonical and extraction coverage."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from models.job_posting import JobPosting, PostingStatus
from storage import GoldStorage, SilverStorage
from utils.config import Config
from utils.logger import log_event

_UNKNOWN_VALUES = {"", "unknown", "n/a", "none", "null", "not specified"}


@dataclass(frozen=True)
class QualityMetric:
    """One evaluated quality threshold."""

    name: str
    value: float | None
    threshold: float
    comparison: str
    passed: bool
    numerator: int | None = None
    denominator: int | None = None
    available: bool = True
    blocking: bool = True

    def summary(self) -> dict[str, object]:
        """Return a JSON-serializable metric explanation."""
        return {
            "name": self.name,
            "value": self.value,
            "threshold": self.threshold,
            "comparison": self.comparison,
            "passed": self.passed,
            "available": self.available,
            "blocking": self.blocking,
            "numerator": self.numerator,
            "denominator": self.denominator,
        }


@dataclass(frozen=True)
class DataQualityResult:
    """Aggregate pass/fail result from one quality evaluation."""

    run_id: str
    generated_at: datetime
    passed: bool
    silver_records_found: int
    valid_records: int
    invalid_records: int
    collection_run_id: str | None
    metrics: list[QualityMetric]
    invalid_record_errors: list[dict[str, str]] = field(default_factory=list)
    output_paths: dict[str, Path] = field(default_factory=dict)

    @property
    def failed_metrics(self) -> list[QualityMetric]:
        """Return blocking metrics that did not meet their threshold."""
        return [metric for metric in self.metrics if metric.blocking and not metric.passed]

    def summary(self) -> dict[str, object]:
        """Return a JSON-serializable quality report."""
        return {
            "run_id": self.run_id,
            "generated_at": self.generated_at.isoformat(),
            "passed": self.passed,
            "silver_records_found": self.silver_records_found,
            "valid_records": self.valid_records,
            "invalid_records": self.invalid_records,
            "collection_run_id": self.collection_run_id,
            "metrics": [metric.summary() for metric in self.metrics],
            "failed_metrics": [metric.name for metric in self.failed_metrics],
            "invalid_record_errors": self.invalid_record_errors,
        }


class DataQualityGateway:
    """Evaluate configured Silver coverage and latest collection reliability."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        silver_storage: SilverStorage | None = None,
        gold_storage: GoldStorage | None = None,
    ) -> None:
        """Initialize shared quality configuration and storage layers."""
        self.config = config or Config()
        self.quality = self.config.data_quality
        self.silver_storage = silver_storage or SilverStorage(
            self.config.settings.paths.silver_dir
        )
        self.gold_storage = gold_storage or GoldStorage(
            self.config.settings.paths.gold_dir
        )

    def run(
        self,
        *,
        collection_summary: Mapping[str, object] | None = None,
        run_id: str | None = None,
        generated_at: datetime | None = None,
    ) -> DataQualityResult:
        """Evaluate every blocking threshold and persist the report."""
        active_run_id = run_id or uuid4().hex
        built_at = generated_at or datetime.now(UTC)
        records, total, errors = self._load_silver()
        market_records = [record for record in records if self._is_market_record(record)]
        operational = collection_summary or self._latest_collection_summary()
        thresholds = self.quality.thresholds

        metrics = [
            self._minimum_metric(
                "minimum_records",
                len(market_records),
                thresholds.minimum_records,
            ),
            self._coverage_metric(
                "valid_title_coverage",
                sum(self._valid_title(record) for record in market_records),
                len(market_records),
                thresholds.valid_title_coverage,
            ),
            self._coverage_metric(
                "known_location_coverage",
                sum(self._known_location(record) for record in market_records),
                len(market_records),
                thresholds.known_location_coverage,
            ),
            self._coverage_metric(
                "description_coverage",
                sum(self._has_description(record) for record in market_records),
                len(market_records),
                thresholds.description_coverage,
            ),
            self._coverage_metric(
                "source_url_coverage",
                sum(record.source.url is not None for record in market_records),
                len(market_records),
                thresholds.source_url_coverage,
            ),
            self._coverage_metric(
                "external_id_coverage",
                sum(bool(record.source.external_id) for record in market_records),
                len(market_records),
                thresholds.external_id_coverage,
            ),
            self._coverage_metric(
                "skill_coverage",
                sum(
                    bool(record.skills.explicit or record.skills.inferred)
                    for record in market_records
                ),
                len(market_records),
                thresholds.skill_coverage,
            ),
            self._maximum_metric(
                "invalid_silver_rate",
                len(errors) / total if total else 0.0,
                thresholds.maximum_invalid_silver_rate,
                numerator=len(errors),
                denominator=total,
            ),
            self._extraction_metric(operational),
        ]

        salary_count = sum(
            record.salary.min_amount is not None or record.salary.max_amount is not None
            for record in market_records
        )
        metrics.append(
            QualityMetric(
                name="salary_coverage",
                value=self._ratio(salary_count, len(market_records)),
                threshold=0.0,
                comparison="informational",
                passed=True,
                numerator=salary_count,
                denominator=len(market_records),
                blocking=False,
            )
        )
        passed = all(metric.passed for metric in metrics if metric.blocking)
        collection_run_id = self._optional_string(
            operational.get("run_id") if operational is not None else None
        )
        result = DataQualityResult(
            run_id=active_run_id,
            generated_at=built_at,
            passed=passed,
            silver_records_found=total,
            valid_records=len(records),
            invalid_records=len(errors),
            collection_run_id=collection_run_id,
            metrics=metrics,
            invalid_record_errors=errors,
        )
        summary = result.summary()
        run_path = self.gold_storage.save(f"data_quality/runs/{active_run_id}", summary)
        latest_path = self.gold_storage.save("data_quality/latest", summary)
        if run_path is None or latest_path is None:
            raise RuntimeError("Gold storage did not return data-quality report paths")
        result = replace(
            result,
            output_paths={"run": run_path, "latest": latest_path},
        )

        log_event(
            "SUCCESS" if passed else "WARNING",
            "Data-quality gate {status}: {failed} blocking metrics failed",
            component="data_quality",
            event="data_quality_gate_finished",
            run_id=active_run_id,
            status="passed" if passed else "failed",
            failed=len(result.failed_metrics),
        )
        return result

    def _load_silver(
        self,
    ) -> tuple[list[JobPosting], int, list[dict[str, str]]]:
        records: list[JobPosting] = []
        errors: list[dict[str, str]] = []
        paths = sorted(self.silver_storage.root.rglob("*.json"))
        for path in paths:
            relative = path.relative_to(self.silver_storage.root).as_posix()
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                records.append(JobPosting.model_validate(payload))
            except Exception as exc:
                errors.append({"record": relative, "error": str(exc)})
        return records, len(paths), errors

    def _latest_collection_summary(self) -> Mapping[str, object] | None:
        if not self.gold_storage.exists("collection/latest"):
            return None
        payload = self.gold_storage.load("collection/latest")
        return payload if isinstance(payload, dict) else None

    def _extraction_metric(
        self, operational: Mapping[str, object] | None
    ) -> QualityMetric:
        threshold = self.quality.thresholds.maximum_extraction_failure_rate
        if operational is None:
            required = self.quality.require_collection_summary
            return QualityMetric(
                name="extraction_failure_rate",
                value=None,
                threshold=threshold,
                comparison="maximum",
                passed=not required,
                available=False,
                blocking=required,
            )
        value = self._optional_number(operational.get("extraction_failure_rate"))
        if value is None:
            return QualityMetric(
                name="extraction_failure_rate",
                value=None,
                threshold=threshold,
                comparison="maximum",
                passed=False,
                available=False,
            )
        failed = (
            self._optional_integer(operational.get("targets_failed"))
            + self._optional_integer(operational.get("search_failures"))
            + self._optional_integer(operational.get("posting_failures"))
        )
        attempted = (
            self._optional_integer(operational.get("targets_attempted"))
            + self._optional_integer(operational.get("search_attempts"))
            + self._optional_integer(operational.get("references_selected"))
        )
        return self._maximum_metric(
            "extraction_failure_rate",
            value,
            threshold,
            numerator=failed,
            denominator=attempted,
        )

    def _has_description(self, record: JobPosting) -> bool:
        value = record.description.clean_text or record.description.raw_text or ""
        return len(value.strip()) >= self.quality.description_minimum_characters

    @staticmethod
    def _is_market_record(record: JobPosting) -> bool:
        return record.metadata.is_active and record.metadata.status not in {
            PostingStatus.DUPLICATE,
            PostingStatus.EXPIRED,
            PostingStatus.REMOVED,
        }

    @staticmethod
    def _valid_title(record: JobPosting) -> bool:
        return record.position.title.strip().casefold() not in _UNKNOWN_VALUES

    @staticmethod
    def _known_location(record: JobPosting) -> bool:
        value = record.location.raw or record.location.city or record.location.region or ""
        return value.strip().casefold() not in _UNKNOWN_VALUES

    @staticmethod
    def _coverage_metric(
        name: str, numerator: int, denominator: int, threshold: float
    ) -> QualityMetric:
        value = DataQualityGateway._ratio(numerator, denominator)
        return QualityMetric(
            name=name,
            value=value,
            threshold=threshold,
            comparison="minimum",
            passed=value >= threshold,
            numerator=numerator,
            denominator=denominator,
        )

    @staticmethod
    def _minimum_metric(name: str, value: int, threshold: int) -> QualityMetric:
        return QualityMetric(
            name=name,
            value=float(value),
            threshold=float(threshold),
            comparison="minimum",
            passed=value >= threshold,
            numerator=value,
        )

    @staticmethod
    def _maximum_metric(
        name: str,
        value: float,
        threshold: float,
        *,
        numerator: int | None = None,
        denominator: int | None = None,
    ) -> QualityMetric:
        return QualityMetric(
            name=name,
            value=round(value, 4),
            threshold=threshold,
            comparison="maximum",
            passed=value <= threshold,
            numerator=numerator,
            denominator=denominator,
        )

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 0.0

    @staticmethod
    def _optional_number(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    @staticmethod
    def _optional_integer(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return value

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return value if isinstance(value, str) and value else None


__all__ = ["DataQualityGateway", "DataQualityResult", "QualityMetric"]
