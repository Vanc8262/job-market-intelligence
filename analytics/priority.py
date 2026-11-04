"""Priority: the relevance score blended with the Phase 6.3 application fit, at read time.

``priority = (1 - fit_weight) * relevance + fit_weight * fit``, both on 0-100. Phase 8 B3
found that application fit predicts which postings you keep beyond relevance, so the
dashboard offers this blend as its own sort. It is computed when the dashboard reads the
mart and never written to Gold, because the fit comes from your private resume.

Every priority carries its parts (relevance, fit, weight, cap) so the number can be
explained. A posting without a comparison falls back to its relevance score, and a capped
relevance score (avoided title, non-home onsite) keeps its cap.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from utils.config import PriorityConfig


def priority_score(
    row: Mapping[str, object], fit: Mapping[str, object] | None, settings: PriorityConfig
) -> dict[str, object] | None:
    """Return the priority of one mart row with what produced it, or None without a score."""
    relevance = _number(row.get("resume_relevance_score"))
    if relevance is None:
        return None
    fit_score = _number(fit.get("fit_score")) if fit else None
    weight = settings.fit_weight
    if fit_score is None:
        score, basis, used_weight = relevance, "relevance_only", 0.0
    else:
        score = (1 - weight) * relevance + weight * fit_score
        basis, used_weight = "blend", weight
    cap = _number(row.get("score_cap"))
    capped = False
    if settings.respect_score_caps and row.get("score_cap_reason") and cap is not None:
        capped = score > cap
        score = min(score, cap)
    return {
        "score": round(score, 2),
        "relevance": round(relevance, 2),
        "fit": round(fit_score, 2) if fit_score is not None else None,
        "fit_weight": used_weight,
        "basis": basis,
        "capped_at": cap if capped else None,
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(float(value)) else None


__all__ = ["priority_score"]
