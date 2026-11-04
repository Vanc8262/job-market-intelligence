"""Priority sort: relevance blended with 6.3 application fit at read time."""

from __future__ import annotations

import numpy as np
import pytest

from analytics.priority import priority_score
from bayesian.relevance import fit_weight_summary
from dashboard.app import attach_application_fit
from utils.config import BayesianRelevanceWeightsConfig, Config, PriorityConfig

SETTINGS = PriorityConfig(fit_weight=0.3)


def test_priority_blends_relevance_with_fit_and_explains_itself() -> None:
    result = priority_score({"resume_relevance_score": 70.0}, {"fit_score": 90.0}, SETTINGS)

    assert result == {
        "score": 76.0,
        "relevance": 70.0,
        "fit": 90.0,
        "fit_weight": 0.3,
        "basis": "blend",
        "capped_at": None,
    }


def test_postings_without_a_comparison_use_relevance_alone() -> None:
    result = priority_score({"resume_relevance_score": 64.5}, None, SETTINGS)

    assert result is not None
    assert (result["score"], result["basis"], result["fit_weight"]) == (64.5, "relevance_only", 0.0)
    assert priority_score({"job_id": "no-score"}, None, SETTINGS) is None


def test_a_capped_relevance_score_stays_capped() -> None:
    row = {
        "resume_relevance_score": 39.0,
        "score_cap": 39.0,
        "score_cap_reason": "avoided_role_type",
    }

    capped = priority_score(row, {"fit_score": 95.0}, SETTINGS)
    uncapped = priority_score(row, {"fit_score": 95.0}, PriorityConfig(respect_score_caps=False))

    assert capped is not None and capped["score"] == 39.0 and capped["capped_at"] == 39.0
    assert uncapped is not None and uncapped["score"] == pytest.approx(55.8)


def test_the_mart_gets_priority_with_or_without_comparisons() -> None:
    mart = {
        "opportunities": [
            {"job_id": "job-1", "resume_relevance_score": 60.0},
            {"job_id": "job-2", "resume_relevance_score": 80.0},
        ]
    }
    comparisons = {"jobs": {"job-1": {"fit_score": 100.0, "band": "strong"}}}

    merged = attach_application_fit(mart, comparisons, SETTINGS)
    bare = attach_application_fit(mart, None, SETTINGS)

    first, second = merged["opportunities"]
    assert first["priority"]["score"] == 72.0 and first["application_fit"]["fit_score"] == 100.0
    assert second["priority"]["basis"] == "relevance_only"
    assert merged["application_fit_available"] is True
    assert merged["priority_settings"]["fit_weight"] == 0.3
    assert bare["application_fit_available"] is False
    assert [row["priority"]["score"] for row in bare["opportunities"]] == [60.0, 80.0]


def test_ranking_config_loads_and_validates() -> None:
    assert 0 <= Config().ranking.priority.fit_weight <= 1
    with pytest.raises(ValueError):
        PriorityConfig(fit_weight=1.5)


def test_b3_reports_the_fit_weight_your_decisions_support() -> None:
    shape = (4, 500)
    draws = {"tau": np.full(shape, 6.0), "gamma": np.full(shape, 2.0)}
    draws["gamma"][0] = -1.0  # one chain where fit points the other way counts as weight 0

    summary = fit_weight_summary(draws, 0.9, 0.1, BayesianRelevanceWeightsConfig())

    assert summary["probability_fit_helps"] == 0.75
    assert summary["high"] == pytest.approx(0.25)
    assert summary["low"] == 0.0
    assert summary["mean"] == pytest.approx(0.75 * 0.25)
    assert summary["probability_above_configured"] == 0.75
    assert summary["verdict"] == "keep"  # 75% above 0.1 is not the 90% needed to raise
