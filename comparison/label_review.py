"""Read model for the Labels tab: every owner label, checked against the latest comparison.

The label log (``comparison/labels.py``) stores what the model said when each label was
made. This module joins the newest label per requirement line with the stored Phase 6.3
results and says how each one stands today:

- ``current``: the line is in the latest comparison and the model still says the same.
- ``stale``: the line is there, but the model's verdict changed since it was labeled.
- ``orphaned``: the posting is still compared but the line is not, usually because a
  parser change reworded or merged it. These raise an alert past a configured count.
- ``closed``: the posting is no longer compared (expired or removed). The label is still
  valid evidence; it just cannot be re-labeled.

A line whose newest entry is a withdrawal is listed with ``withdrawn: true`` and left
out of every count, so it can be found and re-labeled but never skews the numbers.

Nothing here writes; corrections go through ``RequirementLabelStore``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from enum import StrEnum

from comparison.labels import LabelAction, RequirementLabel, find_requirement
from utils.config import LabelsConfig


class LabelState(StrEnum):
    """How a label relates to the latest comparison."""

    CURRENT = "current"
    STALE = "stale"
    ORPHANED = "orphaned"
    CLOSED = "closed"


def review_labels(
    labels: Sequence[RequirementLabel],
    comparisons: Mapping[str, object] | None,
    postings: Mapping[str, Mapping[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Return one row per labeled requirement line, newest first, with its state today.

    ``postings`` maps job ids to display fields (title, employer) from the opportunities
    mart; closed postings are usually missing from it and fall back to the job id.
    """
    history: dict[tuple[str, str], list[RequirementLabel]] = {}
    for label in labels:
        history.setdefault((label.job_id, label.requirement), []).append(label)
    jobs = comparisons.get("jobs") if comparisons else None
    compared = set(jobs) if isinstance(jobs, dict) else set()
    rows: list[dict[str, object]] = []
    for (job_id, requirement), entries in history.items():
        newest = entries[-1]
        withdrawal = newest if newest.action is LabelAction.WITHDRAW else None
        verdicts = [entry for entry in entries if entry.action is LabelAction.LABEL]
        if not verdicts:
            continue
        latest = verdicts[-1]
        corrected = latest.corrected_status.value if latest.corrected_status else None
        item = find_requirement(comparisons, job_id, requirement)
        current_status = str(item.get("status")) if item else None
        if item is not None:
            state = (
                LabelState.CURRENT
                if current_status == latest.predicted_status
                else LabelState.STALE
            )
        elif job_id in compared:
            state = LabelState.ORPHANED
        else:
            state = LabelState.CLOSED
        posting = (postings or {}).get(job_id) or {}
        rows.append(
            {
                "job_id": job_id,
                "title": _optional_str(posting.get("title")),
                "employer": _optional_str(posting.get("employer")),
                "requirement": requirement,
                "kind": latest.kind,
                "required": latest.required,
                "predicted_status": latest.predicted_status,
                "corrected_status": corrected,
                "agrees": latest.agrees,
                "current_status": current_status,
                "agrees_now": current_status == corrected if current_status else None,
                "state": state.value,
                "source": latest.source.value,
                "sampled": latest.sampled,
                "note": latest.note,
                "model_version": latest.model_version,
                "labeled_at": latest.labeled_at.isoformat(),
                "withdrawn": withdrawal is not None,
                "withdrawn_at": withdrawal.labeled_at.isoformat() if withdrawal else None,
                "withdrawal_note": withdrawal.note if withdrawal else None,
                "history": [
                    {
                        "action": entry.action.value,
                        "predicted_status": entry.predicted_status,
                        "corrected_status": entry.corrected_status.value
                        if entry.corrected_status
                        else None,
                        "source": entry.source.value,
                        "note": entry.note,
                        "model_version": entry.model_version,
                        "labeled_at": entry.labeled_at.isoformat(),
                    }
                    for entry in reversed(entries)
                    if entry is not latest and entry is not withdrawal
                ],
            }
        )
    rows.sort(key=lambda row: str(row["withdrawn_at"] or row["labeled_at"]), reverse=True)
    return rows


def orphan_alert(orphaned: int, total: int, settings: LabelsConfig) -> dict[str, object]:
    """Return whether orphaned labels passed the configured count or share threshold."""
    rules = settings.orphans
    share = orphaned / total if total else 0.0
    reasons: list[str] = []
    if orphaned >= rules.alert_count:
        reasons.append(f"{orphaned} orphaned labels (alert at {rules.alert_count})")
    if (
        rules.alert_share is not None
        and total >= rules.share_minimum_labels
        and share >= rules.alert_share
    ):
        reasons.append(f"{share:.0%} of labels orphaned (alert at {rules.alert_share:.0%})")
    return {
        "active": bool(reasons),
        "reasons": reasons,
        "orphaned": orphaned,
        "share": round(share, 4),
        "alert_count": rules.alert_count,
        "alert_share": rules.alert_share,
        "share_minimum_labels": rules.share_minimum_labels,
    }


def summarize_labels(
    rows: Sequence[Mapping[str, object]], settings: LabelsConfig
) -> dict[str, object]:
    """Return counts for the tracker strip: totals, states, sources, kinds, orphan alert.

    Withdrawn lines are counted once, as ``withdrawn``, and nowhere else. Sources are
    always reported separately because only sampled queue labels are unbiased.
    """
    withdrawn = sum(row["withdrawn"] is True for row in rows)
    rows = [row for row in rows if row["withdrawn"] is not True]
    states = Counter(str(row["state"]) for row in rows)
    total = len(rows)
    return {
        "total": total,
        "withdrawn": withdrawn,
        "sampled": sum(row["sampled"] is True for row in rows),
        "agree": sum(row["agrees"] is True for row in rows),
        "disagree": sum(row["agrees"] is False for row in rows),
        "states": {state.value: states.get(state.value, 0) for state in LabelState},
        "by_source": dict(Counter(str(row["source"]) for row in rows)),
        "by_kind": dict(Counter(str(row["kind"] or "unknown") for row in rows)),
        "orphan_alert": orphan_alert(states.get(LabelState.ORPHANED.value, 0), total, settings),
        "queue_target_per_group": settings.queue.target_per_group,
        "settings_version": settings.version,
    }


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


__all__ = ["LabelState", "orphan_alert", "review_labels", "summarize_labels"]
