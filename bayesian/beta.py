"""Beta-Binomial building blocks: conjugate posteriors and empirical-Bayes priors.

A share such as "postings with a known location" is ``k`` successes in ``n`` trials.
With a ``Beta(alpha, beta)`` prior the posterior is ``Beta(alpha + k, beta + n - k)``
in closed form, so no sampler (and no convergence diagnostics) is needed.

When the same share is measured for several groups (employers, role families), an
empirical-Bayes prior is fitted to all groups at once by maximizing the Beta-Binomial
marginal likelihood. Each group's posterior then borrows strength from the others:
small groups move toward the pooled rate, large groups keep their own.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy import optimize, special, stats

JEFFREYS = (0.5, 0.5)


@dataclass(frozen=True)
class BetaPosterior:
    """A Beta distribution over a rate, with the summaries reports need."""

    alpha: float
    beta: float

    @property
    def mean(self) -> float:
        """Posterior mean of the rate."""
        return self.alpha / (self.alpha + self.beta)

    def interval(self, level: float = 0.9) -> tuple[float, float]:
        """Equal-tailed credible interval holding ``level`` of the probability."""
        tail = (1.0 - level) / 2.0
        low, high = stats.beta.ppf([tail, 1.0 - tail], self.alpha, self.beta)
        return float(low), float(high)

    def probability_at_least(self, threshold: float) -> float:
        """Posterior probability that the rate is at least ``threshold``."""
        return float(stats.beta.sf(threshold, self.alpha, self.beta))

    def probability_at_most(self, threshold: float) -> float:
        """Posterior probability that the rate is at most ``threshold``."""
        return float(stats.beta.cdf(threshold, self.alpha, self.beta))

    def summary(self, level: float = 0.9) -> dict[str, float]:
        """Return the mean and credible interval, rounded for reports."""
        low, high = self.interval(level)
        return {"mean": round(self.mean, 4), "low": round(low, 4), "high": round(high, 4)}


def posterior(successes: int, trials: int, prior: tuple[float, float] = JEFFREYS) -> BetaPosterior:
    """Return the conjugate posterior after ``successes`` out of ``trials``."""
    if trials < 0 or successes < 0 or successes > trials:
        raise ValueError("need 0 <= successes <= trials")
    alpha, beta = prior
    if alpha <= 0 or beta <= 0:
        raise ValueError("Beta prior parameters must be positive")
    return BetaPosterior(alpha + successes, beta + trials - successes)


@dataclass(frozen=True)
class EmpiricalPrior:
    """A Beta prior fitted to several groups, described by its mean and strength."""

    alpha: float
    beta: float
    groups: int
    method: str

    @property
    def mean(self) -> float:
        """Pooled rate the groups are shrunk toward."""
        return self.alpha / (self.alpha + self.beta)

    @property
    def strength(self) -> float:
        """Prior pseudo-count: a group with this many trials is shrunk halfway."""
        return self.alpha + self.beta

    def summary(self) -> dict[str, object]:
        """Return a JSON-safe description."""
        return {
            "alpha": round(self.alpha, 4),
            "beta": round(self.beta, 4),
            "mean": round(self.mean, 4),
            "strength": round(self.strength, 2),
            "groups": self.groups,
            "method": self.method,
        }


def _negative_log_marginal(
    parameters: np.ndarray, successes: np.ndarray, trials: np.ndarray
) -> float:
    mean = special.expit(parameters[0])
    strength = math.exp(parameters[1])
    alpha, beta = mean * strength, (1.0 - mean) * strength
    value = special.betaln(successes + alpha, trials - successes + beta) - special.betaln(
        alpha, beta
    )
    return float(-np.sum(value))


def fit_beta_prior(
    successes: Sequence[int],
    trials: Sequence[int],
    *,
    fallback_strength: float = 10.0,
    max_strength: float = 1000.0,
) -> EmpiricalPrior:
    """Fit a Beta prior to grouped counts by maximum marginal likelihood.

    With fewer than three informative groups the prior is centered on the pooled rate
    with ``fallback_strength`` pseudo-counts. The strength is capped at
    ``max_strength`` (groups that agree perfectly would otherwise pool completely).
    """
    k = np.asarray(successes, dtype=np.float64)
    n = np.asarray(trials, dtype=np.float64)
    if k.shape != n.shape or np.any(k < 0) or np.any(k > n):
        raise ValueError("need matching counts with 0 <= successes <= trials")
    informative = n > 0
    k, n = k[informative], n[informative]
    pooled = (k.sum() + 0.5) / (n.sum() + 1.0) if n.size else 0.5
    if k.size < 3:
        strength = fallback_strength
        return EmpiricalPrior(
            alpha=pooled * strength,
            beta=(1.0 - pooled) * strength,
            groups=int(k.size),
            method="pooled_fallback",
        )
    start = np.array([special.logit(min(max(pooled, 1e-3), 1 - 1e-3)), math.log(10.0)])
    result = optimize.minimize(
        _negative_log_marginal,
        start,
        args=(k, n),
        method="L-BFGS-B",
        bounds=[(-12.0, 12.0), (math.log(0.5), math.log(max_strength))],
    )
    mean = float(special.expit(result.x[0]))
    strength = float(math.exp(result.x[1]))
    return EmpiricalPrior(
        alpha=mean * strength,
        beta=(1.0 - mean) * strength,
        groups=int(k.size),
        method="marginal_likelihood",
    )


__all__ = ["JEFFREYS", "BetaPosterior", "EmpiricalPrior", "fit_beta_prior", "posterior"]
