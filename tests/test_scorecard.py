"""Tests for the Labels Scorecard: Beta-Binomial agreement, weighting, and the replay."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

import pytest
from scipy import stats

from comparison import RequirementVocabulary, ResumeView, compare_posting
from comparison.labels import RequirementLabel, RequirementLabelStore, labels_path
from comparison.runner import results_path
from comparison.scorecard import (
    build_scorecard,
    rate,
    replay,
    scored_labels,
    weighted_agreement,
)
from dashboard.app import DashboardHTTPServer, create_server
from processors import JobPostingNormalizer
from profiles.parser import ResumeParser, SkillVocabulary
from tests.support.resumes import SAMPLE_RESUME
from tests.test_comparison import posting
from utils.config import Config, LabelsConfig

START = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def _label(
    job_id: str,
    requirement: str,
    predicted: str,
    corrected: str,
    *,
    kind: str = "skill",
    sampled: bool = True,
    version: str = "1.3.0",
    minutes: int = 0,
) -> RequirementLabel:
    return RequirementLabel(
        source="queue" if sampled else "drawer",
        sampled=sampled,
        sample_seed=7 if sampled else None,
        job_id=job_id,
        requirement=requirement,
        predicted_status=predicted,
        corrected_status=corrected,
        agrees=predicted == corrected,
        kind=kind,
        required=True,
        model_version=version,
        labeled_at=START + timedelta(minutes=minutes),
    )


def _comparisons(skill_met: int = 30, degree_missing: int = 10) -> dict[str, object]:
    """Population: ``skill_met`` skill lines marked met, ``degree_missing`` degree lines."""
    items = [
        {"text": f"Skill line {i}", "kind": "skill", "status": "met", "required": True}
        for i in range(skill_met)
    ] + [
        {"text": f"Degree line {i}", "kind": "degree", "status": "missing", "required": True}
        for i in range(degree_missing)
    ]
    return {
        "model": {"version": "1.4.0"},
        "jobs": {"job-1": {"dimensions": {"requirements": {"items": items}}}},
    }


def _settings(**scorecard: object) -> LabelsConfig:
    return LabelsConfig.model_validate({"scorecard": {"minimum_labels": 5, **scorecard}})


def test_rate_matches_the_hand_computed_jeffreys_posterior() -> None:
    result = rate(8, 10, _settings())

    low, high = stats.beta.ppf([0.05, 0.95], 8.5, 2.5)
    assert result["agree"] == 8 and result["total"] == 10
    assert result["mean"] == round(8.5 / 11, 4)
    assert result["low"] == round(float(low), 4)
    assert result["high"] == round(float(high), 4)
    # Below the minimum only the counts are shown.
    assert rate(3, 4, _settings()) == {"agree": 3, "total": 4}


def test_only_active_scorable_labels_count() -> None:
    labels = [
        _label("job-1", "Skill line 0", "met", "met"),
        _label("job-1", "Skill line 0", "met", "partial", minutes=1),
        _label("job-1", "Curious", "self_assess", "met", kind="trait"),
        RequirementLabel(
            action="withdraw",
            source="labels_tab",
            job_id="job-1",
            requirement="Skill line 1",
            predicted_status="met",
            kind="skill",
            labeled_at=START,
        ),
    ]

    items = scored_labels(labels, _settings())

    assert [(i.label.requirement, i.verdict) for i in items] == [("Skill line 0", "partial")]


def test_overall_agreement_weights_groups_by_population() -> None:
    # Skill/met: 10 of 10 agree; degree/missing: 0 of 10. Population 30 vs 10, so the
    # weighted rate sits near 0.75, not the unweighted 0.5.
    groups = [
        {"kind": "skill", "status": "met", "population": 30, "agree": 10, "total": 10},
        {"kind": "degree", "status": "missing", "population": 10, "agree": 0, "total": 10},
    ]

    overall = weighted_agreement(groups, _settings())

    expected = 0.75 * (10.5 / 11) + 0.25 * (0.5 / 11)
    assert overall["mean"] == pytest.approx(expected, abs=0.005)
    assert overall["low"] < overall["mean"] < overall["high"]
    assert overall["coverage"] == 1.0
    assert (overall["agree"], overall["total"]) == (10, 20)
    # Same seed, same interval.
    assert weighted_agreement(groups, _settings()) == overall


def test_groups_without_labels_reduce_coverage_not_the_estimate() -> None:
    groups = [
        {"kind": "skill", "status": "met", "population": 30, "agree": 9, "total": 10},
        {"kind": "degree", "status": "missing", "population": 10, "agree": 0, "total": 0},
    ]

    overall = weighted_agreement(groups, _settings())

    assert overall["coverage"] == 0.75
    assert overall["groups_used"] == 1
    assert overall["mean"] == pytest.approx(9.5 / 11, abs=0.005)


def test_scorecard_separates_sampled_and_chosen_and_counts_parser_mistakes() -> None:
    labels = [
        *[_label("job-1", f"Skill line {i}", "met", "met", minutes=i) for i in range(6)],
        _label("job-1", "Skill line 6", "met", "not_requirement", minutes=6),
        *[
            _label("job-1", f"Degree line {i}", "missing", "partial", kind="degree", minutes=i)
            for i in range(5)
        ],
        _label("job-1", "Skill line 9", "met", "missing", sampled=False, version="1.2.0"),
    ]

    card = build_scorecard(labels, _comparisons(), _settings())
    sampled = card["sampled"]
    groups = {(g["kind"], g["status"]): g for g in sampled["groups"]}  # type: ignore[index]

    assert sampled["labels"] == 12  # type: ignore[index]
    assert groups[("skill", "met")]["agree"] == 6
    assert groups[("skill", "met")]["total"] == 6  # the ⊘ line is not a verdict
    assert groups[("skill", "met")]["not_requirement"] == 1
    assert groups[("skill", "met")]["population"] == 30
    assert groups[("degree", "missing")]["agree"] == 0
    assert sampled["not_requirement"] == rate(1, 12, _settings(), key="count")  # type: ignore[index]
    assert sampled["not_requirement"]["count"] == 1  # type: ignore[index]
    assert sampled["confusion"]["met"]["not_requirement"] == 1  # type: ignore[index]
    assert sampled["confusion"]["missing"]["partial"] == 5  # type: ignore[index]
    assert card["chosen"]["overall"] == {"agree": 0, "total": 1}  # type: ignore[index]
    versions = {row["model_version"]: row for row in card["by_model_version"]}  # type: ignore[union-attr]
    assert versions["1.2.0"]["chosen"]["total"] == 1
    assert versions["1.3.0"]["sampled"]["total"] == 11
    assert card["replay"] == {"available": False, "reason": "No approved resume"}


def test_replay_compares_then_and_now_on_the_same_lines() -> None:
    labels = [
        _label("job-1", "Fixed line", "missing", "met"),
        _label("job-1", "Regressed line", "met", "met"),
        _label("job-1", "Steady line", "partial", "partial"),
        _label("job-1", "Now a trait", "met", "met"),
        _label("job-1", "Boilerplate", "met", "not_requirement"),
    ]
    verdicts = {
        "Fixed line": "met",
        "Regressed line": "partial",
        "Steady line": "partial",
        "Now a trait": "self_assess",
    }

    result = replay(scored_labels(labels, _settings()), lambda text, _: verdicts[text], _settings())

    assert result["lines"] == 3 and result["dropped"] == 1
    assert result["all"]["then"]["agree"] == 2  # type: ignore[index]
    assert result["all"]["now"]["agree"] == 2  # type: ignore[index]
    assert [row["requirement"] for row in result["fixed"]] == ["Fixed line"]  # type: ignore[union-attr]
    assert [row["requirement"] for row in result["regressed"]] == ["Regressed line"]  # type: ignore[union-attr]
    assert result["regressed"][0]["now_status"] == "partial"  # type: ignore[index]


def test_replay_judge_matches_the_full_comparison() -> None:
    """Judging one line alone gives the verdict compare_posting gives it in context."""
    config = Config()
    parser = ResumeParser(SkillVocabulary(config.skills, config.resume_profile.skills))
    resume = ResumeView.from_facts(parser.parse(SAMPLE_RESUME).facts, SAMPLE_RESUME)
    normalizer = JobPostingNormalizer(config)
    vocabulary = RequirementVocabulary(config.comparison)
    result = compare_posting(
        posting(),
        resume,
        config.comparison,
        skills_in_text=normalizer.skills_in_text,
        frequencies=Counter(),
        corpus_size=1,
        vocabulary=vocabulary,
    )
    items = result["dimensions"]["requirements"]["items"]  # type: ignore[index]

    from comparison.compare import assess_requirement
    from comparison.requirements import classify

    for item in items:
        line = classify(item["text"], item["required"], normalizer.skills_in_text, vocabulary)
        status = assess_requirement(line, resume, config.comparison, vocabulary)["status"]
        assert status == item["status"], item["text"]


@contextmanager
def _running(server: DashboardHTTPServer) -> Iterator[str]:
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_serves_the_scorecard_without_a_resume(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    results = results_path(private_root)
    results.parent.mkdir(parents=True)
    results.write_text(json.dumps(_comparisons()), encoding="utf-8")
    store = RequirementLabelStore(labels_path(private_root))
    for i in range(5):
        store.append(_label("job-1", f"Skill line {i}", "met", "met", minutes=i))
    server = create_server(
        port=0, private_root=private_root, tracking_path=tmp_path / "tracking.json"
    )

    with _running(server) as base_url, urlopen(f"{base_url}/api/labels/scorecard") as response:  # noqa: S310
        card = json.loads(response.read())

    assert card["sampled"]["labels"] == 5
    assert card["sampled"]["overall"]["total"] == 5
    assert card["replay"]["available"] is False
    assert "No approved active resume" in card["replay"]["reason"]
