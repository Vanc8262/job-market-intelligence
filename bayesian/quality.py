"""B1: Beta-Binomial evaluation of the data-quality coverage thresholds.

The rule-based gate compares an observed share with a threshold: 0.948 against 0.95
fails, 0.951 passes, whether it came from 40 postings or 4,000. Here each share gets a
posterior distribution, and the verdict is the probability that the *true* rate meets
the threshold:

* ``pass`` when that probability is at least ``pass_probability`` (0.9 by default),
* ``fail`` when it is at most ``fail_probability`` (0.1),
* ``uncertain`` in between, meaning more data is needed before acting.

Each coverage share is also broken down by employer with an empirical-Bayes prior, so
the report names the employers most likely responsible for a low share, without
over-reacting to an employer with only two postings.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from bayesian.beta import BetaPosterior, fit_beta_prior, posterior
from bayesian.records import model_card
from utils.config import BayesianQualityGateConfig

MODEL_NAME = "quality_gate"


@dataclass(frozen=True)
class RateCheck:
    """One thresholded share: ``successes`` of ``trials``, by employer when known."""

    name: str
    successes: int
    trials: int
    threshold: float
    comparison: str  # "minimum" (coverage), "maximum" (failure rate), or "informational"
    by_group: Mapping[str, tuple[int, int]] = field(default_factory=dict)


def _verdict(probability: float, settings: BayesianQualityGateConfig) -> str:
    if probability >= settings.pass_probability:
        return "pass"
    if probability <= settings.fail_probability:
        return "fail"
    return "uncertain"


def _rule_meets(check: RateCheck, successes: int, trials: int) -> bool:
    observed = successes / trials if trials else 0.0
    if check.comparison == "maximum":
        return observed <= check.threshold
    return observed >= check.threshold


def _boundary(check: RateCheck) -> bool:
    if check.comparison == "informational":
        return False
    if check.comparison == "maximum":
        return check.threshold <= 0.0
    return check.threshold >= 1.0


def _probability_meets(check: RateCheck, alpha: float, beta: float) -> float:
    distribution = BetaPosterior(alpha, beta)
    if check.comparison == "maximum":
        return distribution.probability_at_most(check.threshold)
    return distribution.probability_at_least(check.threshold)


def evaluate_check(
    check: RateCheck, settings: BayesianQualityGateConfig, *, credible_level: float
) -> dict[str, object]:
    """Return the posterior, probability of meeting the threshold, and verdict."""
    prior = (settings.prior_alpha, settings.prior_beta)
    overall = posterior(check.successes, check.trials, prior)
    probability = _probability_meets(check, overall.alpha, overall.beta)
    result: dict[str, object] = {
        "name": check.name,
        "comparison": check.comparison,
        "threshold": check.threshold,
        "successes": check.successes,
        "trials": check.trials,
        "observed": round(check.successes / check.trials, 4) if check.trials else None,
        "posterior": overall.summary(credible_level),
        "probability_meets": round(probability, 4),
        "verdict": _verdict(probability, settings) if check.trials else "uncertain",
    }
    if check.comparison == "informational":
        # No threshold to meet: report the estimate and its uncertainty only.
        result["probability_meets"] = None
        result["verdict"] = "estimate"
    elif _boundary(check):
        # A rate can never be shown to be exactly 100% (or 0%): the posterior gives the
        # threshold itself zero probability, so an all-or-nothing rule stays a rule.
        result["probability_meets"] = None
        result["verdict"] = "pass" if _rule_meets(check, check.successes, check.trials) else "fail"
        result["boundary_threshold"] = True
    groups = {name: counts for name, counts in check.by_group.items() if counts[1] > 0}
    if len(groups) >= 2:
        names = sorted(groups)
        empirical = fit_beta_prior(
            [groups[name][0] for name in names], [groups[name][1] for name in names]
        )
        rows = []
        for name in names:
            successes, trials = groups[name]
            group_posterior = posterior(successes, trials, (empirical.alpha, empirical.beta))
            if trials < settings.minimum_employer_postings:
                continue
            group_probability: float | None = None
            if check.comparison == "informational":
                verdict = "estimate"
            elif _boundary(check):
                verdict = "pass" if _rule_meets(check, successes, trials) else "fail"
            else:
                group_probability = _probability_meets(
                    check, group_posterior.alpha, group_posterior.beta
                )
                verdict = _verdict(group_probability, settings)
            rows.append(
                {
                    "employer": name,
                    "successes": successes,
                    "trials": trials,
                    "observed": round(successes / trials, 4),
                    "posterior": group_posterior.summary(credible_level),
                    "probability_meets": (
                        None if group_probability is None else round(group_probability, 4)
                    ),
                    "verdict": verdict,
                    "_order": group_probability
                    if group_probability is not None
                    else group_posterior.mean,
                }
            )
        if check.comparison == "maximum" and _boundary(check):
            rows.sort(key=lambda row: (-float(str(row["_order"])), str(row["employer"])))
        else:
            rows.sort(key=lambda row: (float(str(row["_order"])), str(row["employer"])))
        for row in rows:
            row.pop("_order")
        result["by_employer"] = {
            "prior": empirical.summary(),
            "likely_below": [row["employer"] for row in rows if row["verdict"] == "fail"],
            "rows": rows,
        }
    return result


def evaluate_quality(
    checks: list[RateCheck],
    settings: BayesianQualityGateConfig,
    *,
    credible_level: float,
    mode: str,
) -> dict[str, object]:
    """Evaluate every check and summarize which ones probably fail."""
    results = [evaluate_check(check, settings, credible_level=credible_level) for check in checks]
    return {
        "model": model_card(
            MODEL_NAME,
            stage="B1",
            settings=settings,
            replaces="fixed ratio thresholds in data_quality.yaml",
            version=settings.version,
        ),
        "mode": mode,
        "credible_level": credible_level,
        "checks": results,
        "probably_failing": [item["name"] for item in results if item["verdict"] == "fail"],
        "uncertain": [item["name"] for item in results if item["verdict"] == "uncertain"],
    }


__all__ = ["RateCheck", "evaluate_check", "evaluate_quality"]
