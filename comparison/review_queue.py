"""Review queue: requirement lines drawn at random for unbiased Phase 7 labels.

Labels made in the drawer come from postings the owner chose to open, which are mostly
strong matches, so their agreement rate overstates how well 6.3 works. The queue draws
lines at random instead, stratified by kind and the model's verdict ("degree lines marked
missing"), until each group has ``queue.target_per_group`` labels.

Each line's place in its group is a hash of the configured seed, the job id, and the
requirement text. That order is reproducible, does not depend on which other postings
exist, and drops new postings into random positions, so the sample stays random as the
comparison is re-run every day. The server serves the first unlabeled line in a group
and accepts a queue label only for a line that is currently first in its group: that
check, not anything the browser sends, is what marks a label as sampled.

A sampled line the owner marks ``not_requirement`` still counts toward its group: it was
drawn at random, so the share of such lines estimates how often the parser picks up
non-requirements in that group.

Phase 7 should weight each group by its ``population`` when combining group accuracies
into one number, because groups are sampled to equal size, not in proportion.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from comparison.labels import LabelAction, RequirementLabel
from utils.config import LabelsConfig

QUEUE_VERDICTS = ("met", "partial", "missing")


@dataclass(frozen=True)
class QueueCandidate:
    """One requirement line that the queue may serve."""

    job_id: str
    requirement: str
    kind: str
    status: str
    rank: str
    item: Mapping[str, object]


def sample_rank(seed: int, job_id: str, requirement: str) -> str:
    """Return the line's fixed random position key within its group."""
    return hashlib.sha256(f"{seed}\0{job_id}\0{requirement}".encode()).hexdigest()


def queue_candidates(
    comparisons: Mapping[str, object] | None, settings: LabelsConfig
) -> dict[tuple[str, str], list[QueueCandidate]]:
    """Group every checkable line in the latest comparison, each group in random order."""
    rules = settings.queue
    jobs = comparisons.get("jobs") if comparisons else None
    groups: dict[tuple[str, str], list[QueueCandidate]] = {}
    for job_id, detail in (jobs or {}).items() if isinstance(jobs, dict) else []:
        dimensions = detail.get("dimensions") if isinstance(detail, dict) else None
        section = dimensions.get("requirements") if isinstance(dimensions, dict) else None
        items = section.get("items") if isinstance(section, dict) else None
        seen: set[str] = set()
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            text, kind, status = item.get("text"), item.get("kind"), item.get("status")
            if not isinstance(text, str) or not isinstance(kind, str) or text in seen:
                continue
            if kind in rules.exclude_kinds or status not in QUEUE_VERDICTS:
                continue
            seen.add(text)
            groups.setdefault((kind, str(status)), []).append(
                QueueCandidate(
                    job_id=str(job_id),
                    requirement=text,
                    kind=kind,
                    status=str(status),
                    rank=sample_rank(rules.seed, str(job_id), text),
                    item=item,
                )
            )
    for candidates in groups.values():
        candidates.sort(key=lambda candidate: candidate.rank)
    return groups


def sampled_lines(labels: Sequence[RequirementLabel]) -> dict[tuple[str, str], RequirementLabel]:
    """Return the lines whose newest entry is an active, sampled queue label."""
    newest: dict[tuple[str, str], RequirementLabel] = {}
    for label in labels:
        newest[(label.job_id, label.requirement)] = label
    return {
        key: label
        for key, label in newest.items()
        if label.action is LabelAction.LABEL and label.sampled
    }


def build_queue(
    comparisons: Mapping[str, object] | None,
    labels: Sequence[RequirementLabel],
    settings: LabelsConfig,
    postings: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Return group progress, the head line of each open group, and the next line to label.

    A group's sampled count includes labels on postings that have since closed: they were
    drawn at random and stay valid. A group's target is capped at the lines it can offer.
    """
    rules = settings.queue
    candidates = queue_candidates(comparisons, settings)
    sampled = sampled_lines(labels)
    labeled = Counter(
        (label.kind or "unknown", label.predicted_status) for label in sampled.values()
    )
    groups: list[dict[str, object]] = []
    heads: dict[tuple[str, str], QueueCandidate] = {}
    for key in sorted(set(candidates) | set(labeled)):
        pool = candidates.get(key, [])
        unlabeled = [c for c in pool if (c.job_id, c.requirement) not in sampled]
        done = labeled.get(key, 0)
        target = min(rules.target_per_group, done + len(unlabeled))
        if done < target:
            heads[key] = unlabeled[0]
        groups.append(
            {
                "kind": key[0],
                "status": key[1],
                "labeled": done,
                "target": target,
                "population": len(pool),
            }
        )
    open_groups = sorted(
        heads,
        key=lambda key: (labeled.get(key, 0) / max(1, rules.target_per_group), key),
    )
    head = heads[open_groups[0]] if open_groups else None
    return {
        "seed": rules.seed,
        "target_per_group": rules.target_per_group,
        "exclude_kinds": list(rules.exclude_kinds),
        "labeled": sum(int(str(group["labeled"])) for group in groups),
        "target": sum(int(str(group["target"])) for group in groups),
        "groups": groups,
        "heads": {(c.job_id, c.requirement) for c in heads.values()},
        "next": _describe(head, postings) if head else None,
        "complete": head is None,
    }


def is_queue_head(queue: Mapping[str, object], job_id: str, requirement: str) -> bool:
    """Return whether the line is currently first in an open group (so it may be sampled)."""
    heads = queue.get("heads")
    return isinstance(heads, set) and (job_id, requirement) in heads


def public_queue(queue: Mapping[str, object]) -> dict[str, object]:
    """Return the queue without the internal head set, ready for JSON."""
    return {key: value for key, value in queue.items() if key != "heads"}


def _describe(
    candidate: QueueCandidate, postings: Mapping[str, Mapping[str, object]] | None
) -> dict[str, object]:
    posting = (postings or {}).get(candidate.job_id) or {}
    title = posting.get("title")
    employer = posting.get("employer")
    return {
        "job_id": candidate.job_id,
        "title": title if isinstance(title, str) else None,
        "employer": employer if isinstance(employer, str) else None,
        "requirement": candidate.requirement,
        "kind": candidate.kind,
        "status": candidate.status,
        "group": {"kind": candidate.kind, "status": candidate.status},
        "item": dict(candidate.item),
    }


__all__ = [
    "QUEUE_VERDICTS",
    "QueueCandidate",
    "build_queue",
    "is_queue_head",
    "public_queue",
    "queue_candidates",
    "sample_rank",
    "sampled_lines",
]
