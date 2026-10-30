"""Scorecard (Phase 7.2): how often the 6.3 verdicts agree with the owner's labels.

Agreement is a rate, so each one is a Beta-Binomial posterior (``bayesian/beta.py``)
reported with a credible interval, never a bare percentage:

- **Sampled labels** (drawn at random by the review queue) give the accuracy estimate.
  They are stratified by kind and the model's verdict and every group is sampled to the
  same size, so the overall rate weights each group by its ``population`` (lines in the
  latest comparison), drawing from every group's posterior to get one interval.
- **Labels from postings you chose** (drawer and Labels tab) are reported separately with
  a plain pooled posterior; they lean toward strong matches and are not an estimate.
- Lines marked ``not_requirement`` are extraction errors, not verdicts: they are left out
  of agreement and reported as the share of sampled lines the parser should not have
  picked up.

The **replay** re-runs the current classifier and matcher with the approved resume over
every labeled line, including lines on postings that closed, so a vocabulary change can
be measured against all labels at once ("the model now agrees on 81% of lines; when you
labeled them it agreed on 76%"). It returns verdicts only, never resume evidence.

Nothing here writes. Inputs are the label log, the latest comparison (for populations),
and, for the replay, a function that judges one line.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from bayesian.beta import posterior
from comparison.compare import ResumeView, assess_requirement
from comparison.labels import LabelAction, RequirementLabel, RequirementVerdict
from comparison.requirements import classify
from comparison.review_queue import QUEUE_VERDICTS, build_queue
from comparison.vocabulary import RequirementVocabulary
from processors import JobPostingNormalizer
from profiles.profile_source import approved_resume_extraction
from profiles.service import ResumeIngestionService
from utils.config import Config, LabelsConfig

YOUR_VERDICTS = tuple(verdict.value for verdict in RequirementVerdict)

# Judges one labeled line with the current model: (requirement text, required) -> status.
Judge = Callable[[str, bool], str]


@dataclass(frozen=True)
class ScoredLabel:
    """The newest active label on one line, in the Scorecard's terms."""

    label: RequirementLabel

    @property
    def group(self) -> tuple[str, str]:
        """The queue group: kind and the model's verdict when the line was labeled."""
        return (self.label.kind or "unknown", self.label.predicted_status)

    @property
    def verdict(self) -> str:
        """The owner's verdict."""
        status = self.label.corrected_status
        return status.value if status else ""


def scored_labels(labels: Sequence[RequirementLabel], settings: LabelsConfig) -> list[ScoredLabel]:
    """Return the newest active label per line whose prediction can be checked.

    Withdrawn lines, excluded kinds (traits), and lines the model left to self-assess are
    dropped: there is no ✓/◐/✗ verdict to agree with.
    """
    newest: dict[tuple[str, str], RequirementLabel] = {}
    for label in labels:
        newest[(label.job_id, label.requirement)] = label
    return [
        ScoredLabel(label)
        for label in newest.values()
        if label.action is LabelAction.LABEL
        and (label.kind or "unknown") not in settings.queue.exclude_kinds
        and label.predicted_status in QUEUE_VERDICTS
    ]


def rate(
    agree: int, total: int, settings: LabelsConfig, *, key: str = "agree"
) -> dict[str, object]:
    """Return counts and, once there are enough labels, the posterior summary."""
    rules = settings.scorecard
    result: dict[str, object] = {key: agree, "total": total}
    if total >= rules.minimum_labels:
        result.update(posterior(agree, total, rules.prior).summary(rules.credible_level))
    return result


def _agreement(items: Sequence[ScoredLabel], settings: LabelsConfig) -> dict[str, object]:
    judged = [item for item in items if not item.label.not_requirement]
    return rate(sum(item.label.agrees is True for item in judged), len(judged), settings)


def confusion(items: Sequence[ScoredLabel]) -> dict[str, dict[str, int]]:
    """Return predicted (rows) by your verdict (columns), including not_requirement."""
    counts = Counter((item.label.predicted_status, item.verdict) for item in items)
    return {
        predicted: {yours: counts.get((predicted, yours), 0) for yours in YOUR_VERDICTS}
        for predicted in QUEUE_VERDICTS
    }


def weighted_agreement(
    groups: Sequence[Mapping[str, object]], settings: LabelsConfig
) -> dict[str, object]:
    """Combine group posteriors into one rate, weighting each group by its population.

    Groups without a judged label cannot be estimated; ``coverage`` is the share of the
    population the estimate stands for, so a high rate over a small coverage is visible.
    """
    rules = settings.scorecard
    usable = [g for g in groups if int(str(g["total"])) > 0 and int(str(g["population"])) > 0]
    population = sum(int(str(g["population"])) for g in groups)
    covered = sum(int(str(g["population"])) for g in usable)
    result: dict[str, object] = {
        "groups_used": len(usable),
        "groups": len(groups),
        "coverage": round(covered / population, 4) if population else 0.0,
        "agree": sum(int(str(g["agree"])) for g in usable),
        "total": sum(int(str(g["total"])) for g in usable),
    }
    if not usable or int(str(result["total"])) < rules.minimum_labels:
        return result
    rng = np.random.default_rng(rules.seed)
    alpha0, beta0 = rules.prior
    draws = np.zeros(rules.draws)
    for group in usable:
        agree, total = int(str(group["agree"])), int(str(group["total"]))
        weight = int(str(group["population"])) / covered
        draws += weight * rng.beta(alpha0 + agree, beta0 + total - agree, rules.draws)
    tail = (1.0 - rules.credible_level) / 2.0
    low, high = np.quantile(draws, [tail, 1.0 - tail])
    result.update(mean=round(float(draws.mean()), 4), low=round(float(low), 4))
    result["high"] = round(float(high), 4)
    return result


def sampled_section(
    items: Sequence[ScoredLabel],
    populations: Mapping[tuple[str, str], int],
    settings: LabelsConfig,
) -> dict[str, object]:
    """Agreement per kind × verdict group, per kind, and overall, from sampled labels."""
    by_group: dict[tuple[str, str], list[ScoredLabel]] = {}
    for item in items:
        by_group.setdefault(item.group, []).append(item)
    groups: list[dict[str, object]] = []
    for key in sorted(set(by_group) | set(populations)):
        members = by_group.get(key, [])
        groups.append(
            {
                "kind": key[0],
                "status": key[1],
                "population": populations.get(key, 0),
                "not_requirement": sum(item.label.not_requirement for item in members),
                **_agreement(members, settings),
            }
        )
    kinds = sorted({str(group["kind"]) for group in groups})
    by_kind = {
        kind: weighted_agreement([g for g in groups if g["kind"] == kind], settings)
        for kind in kinds
    }
    judged = sum(int(str(g["total"])) for g in groups)
    extraction_errors = sum(int(str(g["not_requirement"])) for g in groups)
    return {
        "labels": len(items),
        "overall": weighted_agreement(groups, settings),
        "by_kind": by_kind,
        "groups": groups,
        "confusion": confusion(items),
        "not_requirement": rate(
            extraction_errors, judged + extraction_errors, settings, key="count"
        ),
    }


def chosen_section(items: Sequence[ScoredLabel], settings: LabelsConfig) -> dict[str, object]:
    """Pooled agreement on labels from postings you chose (not an accuracy estimate)."""
    kinds = sorted({item.group[0] for item in items})
    return {
        "labels": len(items),
        "overall": _agreement(items, settings),
        "by_kind": {
            kind: _agreement([item for item in items if item.group[0] == kind], settings)
            for kind in kinds
        },
        "confusion": confusion(items),
        "not_requirement": sum(item.label.not_requirement for item in items),
    }


def by_model_version(
    items: Sequence[ScoredLabel], settings: LabelsConfig
) -> list[dict[str, object]]:
    """Agreement at labeling time, per comparison model version the label was made under."""
    versions = sorted({item.label.model_version or "unknown" for item in items})
    rows: list[dict[str, object]] = []
    for version in versions:
        members = [item for item in items if (item.label.model_version or "unknown") == version]
        rows.append(
            {
                "model_version": version,
                "sampled": _agreement([m for m in members if m.label.sampled], settings),
                "chosen": _agreement([m for m in members if not m.label.sampled], settings),
            }
        )
    return rows


def replay(items: Sequence[ScoredLabel], judge: Judge, settings: LabelsConfig) -> dict[str, object]:
    """Re-judge every labeled line with the current model and compare with then.

    ``then`` is agreement when each line was labeled; ``now`` is the current model's
    agreement on the same lines. Lines the model now leaves to self-assess drop out of
    both, so the two rates always cover the same lines.
    """
    judged = [item for item in items if not item.label.not_requirement]
    rows: list[tuple[ScoredLabel, str]] = []
    for item in judged:
        status = judge(item.label.requirement, item.label.required is not False)
        if status in QUEUE_VERDICTS:
            rows.append((item, status))
    fixed: list[dict[str, object]] = []
    regressed: list[dict[str, object]] = []
    for item, status in rows:
        then, now = item.label.agrees is True, status == item.verdict
        if then != now:
            (fixed if now else regressed).append(
                {
                    "job_id": item.label.job_id,
                    "requirement": item.label.requirement,
                    "kind": item.label.kind,
                    "sampled": item.label.sampled,
                    "then_status": item.label.predicted_status,
                    "now_status": status,
                    "your_status": item.verdict,
                    "model_version": item.label.model_version,
                }
            )

    def pair(selected: Sequence[tuple[ScoredLabel, str]]) -> dict[str, object]:
        return {
            "then": rate(sum(i.label.agrees is True for i, _ in selected), len(selected), settings),
            "now": rate(sum(s == i.verdict for i, s in selected), len(selected), settings),
        }

    limit = settings.scorecard.replay_changes_limit
    return {
        "available": True,
        "lines": len(rows),
        "dropped": len(judged) - len(rows),
        "all": pair(rows),
        "sampled": pair([row for row in rows if row[0].label.sampled]),
        "chosen": pair([row for row in rows if not row[0].label.sampled]),
        "fixed_count": len(fixed),
        "regressed_count": len(regressed),
        "fixed": fixed[:limit],
        "regressed": regressed[:limit],
    }


def resume_judge(config: Config, private_root: Path) -> tuple[Judge | None, str | None]:
    """Return a judge backed by the current config and the approved resume, or why not.

    Classification and matching are the same code the comparison runner uses, applied
    one line at a time; the judge returns only the status, never resume evidence.
    """
    extraction = approved_resume_extraction(config, private_root=private_root)
    if extraction is None:
        return None, "No approved active resume to replay against"
    service = ResumeIngestionService(config, private_root=private_root)
    text = service.extracted_text(extraction.resume_version_id, extraction.text_sha256)
    resume = ResumeView.from_facts(extraction.facts, text)
    settings = config.comparison
    vocabulary = RequirementVocabulary(settings)
    skills_in_text = JobPostingNormalizer(config).skills_in_text

    def judge(requirement: str, required: bool) -> str:
        line = classify(requirement[:500], required, skills_in_text, vocabulary)
        return str(assess_requirement(line, resume, settings, vocabulary)["status"])

    return judge, None


def build_scorecard(
    labels: Sequence[RequirementLabel],
    comparisons: Mapping[str, object] | None,
    settings: LabelsConfig,
    *,
    judge: Judge | None = None,
    replay_unavailable: str | None = None,
) -> dict[str, object]:
    """Return the Scorecard payload: sampled estimate, chosen counts, versions, replay."""
    items = scored_labels(labels, settings)
    queue = build_queue(comparisons, labels, settings)
    groups = queue["groups"]
    populations = {
        (str(g["kind"]), str(g["status"])): int(str(g["population"]))
        for g in (groups if isinstance(groups, list) else [])
    }
    model = comparisons.get("model") if comparisons else None
    return {
        "credible_level": settings.scorecard.credible_level,
        "prior": list(settings.scorecard.prior),
        "minimum_labels": settings.scorecard.minimum_labels,
        "comparison_model": model if isinstance(model, dict) else None,
        "sampled": sampled_section([i for i in items if i.label.sampled], populations, settings),
        "chosen": chosen_section([i for i in items if not i.label.sampled], settings),
        "by_model_version": by_model_version(items, settings),
        "replay": replay(items, judge, settings)
        if judge is not None
        else {"available": False, "reason": replay_unavailable or "No approved resume"},
        "settings_version": settings.version,
    }


__all__ = [
    "YOUR_VERDICTS",
    "Judge",
    "ScoredLabel",
    "build_scorecard",
    "by_model_version",
    "chosen_section",
    "confusion",
    "rate",
    "replay",
    "resume_judge",
    "sampled_section",
    "scored_labels",
    "weighted_agreement",
]
