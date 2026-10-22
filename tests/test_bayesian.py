"""Phase 8 B0-B1: diagnostics gate, Beta-Binomial tools, quality gate, skill shrinkage."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from bayesian import (
    DiagnosticThresholds,
    RateCheck,
    check_draws,
    check_inference_data,
    ess_bulk,
    ess_tail,
    evaluate_quality,
    fit_beta_prior,
    posterior,
    rhat,
    skill_share_report,
)
from utils.config import BayesianQualityGateConfig, BayesianSkillSharesConfig

RNG = np.random.default_rng(20261022)


def ar1(phi: float, chains: int = 4, draws: int = 1000) -> np.ndarray:
    values = np.zeros((chains, draws))
    noise = RNG.normal(size=(chains, draws))
    for step in range(1, draws):
        values[:, step] = phi * values[:, step - 1] + noise[:, step]
    return values


# B0: diagnostics --------------------------------------------------------------------


def test_well_mixed_chains_pass_the_gate() -> None:
    report = check_draws({"mu": RNG.normal(size=(4, 1000)), "beta": RNG.normal(size=(4, 1000, 2))})

    assert report.passed, report.failures
    assert [item.name for item in report.parameters] == ["mu", "beta[0]", "beta[1]"]
    summary = report.summary()
    assert float(str(summary["max_rhat"])) < 1.01
    assert float(str(summary["min_ess_bulk"])) > 2000


def test_gate_catches_each_kind_of_failure() -> None:
    sticky = check_draws({"sticky": ar1(0.97)})
    assert not sticky.passed
    assert any("bulk ESS" in failure for failure in sticky.failures)

    stuck = RNG.normal(size=(4, 1000)) + np.array([[0.0], [0.0], [0.0], [1.0]])
    assert any("R-hat" in failure for failure in check_draws({"stuck": stuck}).failures)

    wide_tail = RNG.normal(size=(4, 1000)) * np.array([[1.0], [1.0], [1.0], [4.0]])
    assert rhat(wide_tail) > 1.01  # the folded R-hat catches a scale mismatch

    divergent = check_draws({"mu": RNG.normal(size=(4, 1000))}, divergences=3)
    assert divergent.failures == ["3 divergent transitions > 0"]
    relaxed = DiagnosticThresholds(max_divergences=5)
    assert check_draws({"mu": RNG.normal(size=(4, 1000))}, divergences=3, thresholds=relaxed).passed


def test_bad_draws_are_rejected() -> None:
    with pytest.raises(ValueError, match="NaN"):
        rhat(np.array([[0.0, 1.0, np.nan, 2.0, 3.0]]))
    with pytest.raises(ValueError, match="at least 4"):
        ess_bulk(np.array([[1.0, 2.0]]))


def test_matches_arviz_when_installed() -> None:
    arviz = pytest.importorskip("arviz")
    for draws in (RNG.normal(size=(4, 1000)), ar1(0.9), RNG.gamma(2.0, size=(3, 501))):
        assert rhat(draws) == pytest.approx(float(arviz.rhat(draws)), rel=1e-6)
        assert ess_bulk(draws) == pytest.approx(float(arviz.ess(draws, method="bulk")), rel=1e-6)
        expected_tail = float(arviz.ess(draws, method="tail", prob=(0.05, 0.95)))
        assert ess_tail(draws) == pytest.approx(expected_tail, rel=1e-6)


def test_inference_data_adapter_reads_posterior_and_divergences() -> None:
    class Group(dict[str, Any]):
        """Stands in for an ArviZ group: variables with ``.values``, plus ``data_vars``."""

        @property
        def data_vars(self) -> list[str]:
            return list(self)

    def variable(values: np.ndarray) -> SimpleNamespace:
        return SimpleNamespace(values=values)

    diverging = np.zeros((4, 1000), dtype=bool)
    diverging[0, :2] = True
    idata = {
        "posterior": Group(mu=variable(RNG.normal(size=(4, 1000)))),
        "sample_stats": Group(diverging=variable(diverging)),
    }

    report = check_inference_data(idata)

    assert report.divergences == 2
    assert report.failures == ["2 divergent transitions > 0"]


# B0: Beta-Binomial ------------------------------------------------------------------


def test_conjugate_posterior() -> None:
    result = posterior(45, 50)
    assert (result.alpha, result.beta) == (45.5, 5.5)
    low, high = result.interval(0.9)
    assert low < result.mean < high
    assert result.probability_at_least(0.5) > 0.999
    with pytest.raises(ValueError):
        posterior(5, 3)


def test_empirical_prior_recovers_the_population() -> None:
    rates = RNG.beta(20, 80, size=300)
    trials = np.full(300, 60)
    successes = RNG.binomial(trials, rates)

    prior = fit_beta_prior(successes.tolist(), trials.tolist())

    assert prior.method == "marginal_likelihood"
    assert prior.mean == pytest.approx(0.2, abs=0.02)
    assert 50 < prior.strength < 200  # true strength 100

    assert fit_beta_prior([3, 4], [10, 10]).method == "pooled_fallback"
    identical = fit_beta_prior([50, 50, 50, 50], [100, 100, 100, 100], max_strength=500)
    assert identical.strength == pytest.approx(500, rel=0.01)


# B1: quality gate --------------------------------------------------------------------

GATE = BayesianQualityGateConfig()


def verdicts(*checks: RateCheck) -> dict[str, object]:
    report = evaluate_quality(list(checks), GATE, credible_level=0.9, mode="advisory")
    return {str(item["name"]): item for item in report["checks"]}  # type: ignore[union-attr]


def test_sample_size_changes_the_verdict() -> None:
    results = verdicts(
        RateCheck("small_miss", 47, 50, 0.95, "minimum"),
        RateCheck("large_miss", 940, 1000, 0.95, "minimum"),
        RateCheck("clear_pass", 4975, 5000, 0.98, "minimum"),
        RateCheck("failure_rate", 2, 200, 0.05, "maximum"),
        RateCheck("all_or_nothing", 9, 10, 1.0, "minimum"),
    )

    assert results["small_miss"]["verdict"] == "uncertain"  # 94% of 50 could be 95%
    assert results["large_miss"]["verdict"] == "fail"
    assert results["clear_pass"]["verdict"] == "pass"
    assert results["failure_rate"]["verdict"] == "pass"
    boundary = results["all_or_nothing"]
    assert boundary["verdict"] == "fail" and boundary["probability_meets"] is None


def test_employer_breakdown_names_the_likely_cause() -> None:
    groups = {f"employer-{index}": (40, 40) for index in range(6)}
    groups["broken"] = (8, 30)
    groups["tiny"] = (1, 2)
    successes = sum(hits for hits, _ in groups.values())
    trials = sum(total for _, total in groups.values())

    result = verdicts(RateCheck("location", successes, trials, 0.95, "minimum", groups))

    breakdown = result["location"]["by_employer"]
    assert isinstance(breakdown, dict)
    assert breakdown["likely_below"] == ["broken"]
    rows = breakdown["rows"]
    assert rows[0]["employer"] == "broken"
    assert "tiny" not in {row["employer"] for row in rows}  # below the listing minimum


# B1: skill shares --------------------------------------------------------------------


def test_small_families_are_shrunk_toward_the_typical_share() -> None:
    postings: list[tuple[str | None, set[str]]] = []
    for family, total, with_python in (
        ("software_engineering", 200, 100),
        ("data_engineering", 150, 75),
        ("machine_learning", 120, 60),
        ("data_science", 6, 6),
        (None, 5, 0),
    ):
        postings += [(family, {"Python"}) for _ in range(with_python)]
        postings += [(family, {"Java"}) for _ in range(total - with_python)]

    report = skill_share_report(
        postings, BayesianSkillSharesConfig(minimum_family_postings=5), credible_level=0.9
    )

    python = next(row for row in report["skills"] if row["skill"] == "Python")  # type: ignore[union-attr]
    by_family = python["by_family"]
    small = by_family["data_science"]
    assert small["raw_share"] == 1.0
    assert 0.5 < small["mean"] < 0.9  # six of six is not "everyone"
    large = by_family["software_engineering"]
    assert large["mean"] == pytest.approx(0.5, abs=0.02)
    assert small["high"] - small["low"] > large["high"] - large["low"]
    assert {item["family"] for item in report["families"]} >= {"data_science", "unclassified"}  # type: ignore[union-attr]
    assert python["interval"]["low"] < python["share"] < python["interval"]["high"]
