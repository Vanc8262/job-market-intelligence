"""Phase 7 checks for models that predict probabilities: PSIS-LOO, Brier, reliability.

Every number here comes from **leave-one-out** predictions, never in-sample fits, so a
model is not rewarded for memorizing its own labels:

* ``psis_loo`` - Pareto-smoothed importance-sampling LOO (ArviZ) from a pointwise
  log-likelihood array, with the Pareto k of each row.
* ``elpd_difference`` - the paired ELPD difference of two models on the same rows and its
  standard error (Vehtari, Gelman, and Gabry 2017).
* ``brier`` - the mean squared error of LOO probabilities, with its standard error.
* ``reliability_bins`` - rows grouped by LOO probability; each group's observed rate is a
  Beta-Binomial posterior to set beside the mean predicted probability.

For a yes/no outcome the LOO predictive density of the observed label *is* the LOO
probability of that label, so ``loo_probability`` needs no extra importance weights.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from bayesian.beta import JEFFREYS, posterior

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class LooResult:
    """Pointwise leave-one-out fit of one model."""

    elpd_i: FloatArray
    pareto_k: FloatArray
    # Rows whose PSIS estimate was replaced by an exact refit without that row.
    exact_rows: tuple[int, ...] = ()

    @property
    def elpd(self) -> float:
        """Expected log predictive density summed over rows."""
        return float(self.elpd_i.sum())

    @property
    def se(self) -> float:
        """Standard error of the ELPD."""
        return float(math.sqrt(len(self.elpd_i) * float(np.var(self.elpd_i))))

    def high_k_rows(self, threshold: float) -> list[int]:
        """Rows whose PSIS estimate is unreliable and that no exact refit replaced."""
        return [
            int(index)
            for index in np.flatnonzero(self.pareto_k > threshold)
            if int(index) not in self.exact_rows
        ]

    def with_exact(self, row: int, elpd: float) -> LooResult:
        """Return a copy with one row's estimate replaced by an exact refit."""
        values = self.elpd_i.copy()
        values[row] = elpd
        return LooResult(values, self.pareto_k, tuple(sorted({*self.exact_rows, row})))

    def summary(self, threshold: float) -> dict[str, object]:
        """Return a JSON-safe summary for reports."""
        high = self.high_k_rows(threshold)
        return {
            "elpd": round(self.elpd, 3),
            "se": round(self.se, 3),
            "max_pareto_k": round(float(self.pareto_k.max(initial=0.0)), 3),
            "rows_above_k": int((self.pareto_k > threshold).sum()),
            "exact_refits": len(self.exact_rows),
            "reliable": not high,
        }


def psis_loo(log_likelihood: Any) -> LooResult:  # noqa: ANN401 - (chains, draws, rows)
    """Run PSIS-LOO (ArviZ, from the optional ``bayes`` extra) on a log-likelihood array."""
    import arviz as az  # noqa: PLC0415 - optional dependency

    values = np.asarray(log_likelihood, dtype=np.float64)
    if values.ndim != 3:
        raise ValueError("log_likelihood must be (chains, draws, rows)")
    # PSIS needs a posterior group; a constant placeholder keeps ArviZ's checks satisfied.
    tree = az.from_dict(
        {
            "posterior": {"placeholder": np.zeros(values.shape[:2])},
            "log_likelihood": {"y": values},
        }
    )
    result = az.loo(tree, pointwise=True)
    return LooResult(
        elpd_i=np.asarray(result.elpd_i, dtype=np.float64).reshape(-1),
        pareto_k=np.asarray(result.pareto_k, dtype=np.float64).reshape(-1),
    )


def exact_elpd(log_likelihood_row: Any) -> float:  # noqa: ANN401 - draws of one held-out row
    """Return log mean p(y_i | theta) over posterior draws fitted without row i."""
    values = np.asarray(log_likelihood_row, dtype=np.float64).reshape(-1)
    peak = float(values.max())
    return float(peak + math.log(float(np.exp(values - peak).mean())))


def elpd_difference(better: LooResult, other: LooResult) -> dict[str, float]:
    """Return ELPD(better) - ELPD(other) on the same rows, with its paired standard error."""
    if better.elpd_i.shape != other.elpd_i.shape:
        raise ValueError("ELPD differences need both models fitted on the same rows")
    difference = better.elpd_i - other.elpd_i
    return {
        "difference": round(float(difference.sum()), 3),
        "se": round(float(math.sqrt(len(difference) * float(np.var(difference)))), 3),
    }


def loo_probability(loo: LooResult, labels: Sequence[int]) -> FloatArray:
    """Return each row's LOO probability of a positive label."""
    observed = np.asarray(labels, dtype=np.float64)
    density = np.exp(loo.elpd_i)
    return np.where(observed == 1, density, 1.0 - density)


def brier(probabilities: Sequence[float] | FloatArray, labels: Sequence[int]) -> dict[str, float]:
    """Return the Brier score of probabilities against 0/1 labels, with its standard error."""
    predicted = np.asarray(probabilities, dtype=np.float64)
    observed = np.asarray(labels, dtype=np.float64)
    if predicted.shape != observed.shape or predicted.size == 0:
        raise ValueError("need one probability per label")
    errors = (predicted - observed) ** 2
    return {
        "brier": round(float(errors.mean()), 4),
        "se": round(
            float(errors.std(ddof=1) / math.sqrt(errors.size)) if errors.size > 1 else 0.0, 4
        ),
    }


def reliability_bins(
    probabilities: Sequence[float] | FloatArray,
    labels: Sequence[int],
    *,
    bins: int,
    level: float = 0.9,
    prior: tuple[float, float] = JEFFREYS,
) -> list[dict[str, object]]:
    """Group rows into about equal-count bins by probability; compare predicted with observed.

    Bin edges are quantiles of the probabilities, and tied probabilities always share a
    bin, so a model that predicts few distinct values yields fewer, honest bins.
    """
    predicted = np.asarray(probabilities, dtype=np.float64)
    observed = np.asarray(labels, dtype=np.int64)
    if predicted.shape != observed.shape:
        raise ValueError("need one probability per label")
    if predicted.size == 0:
        return []
    edges = np.quantile(predicted, np.linspace(0.0, 1.0, max(1, bins) + 1)[1:-1])
    assignment = np.searchsorted(edges, predicted, side="left")
    groups = [np.flatnonzero(assignment == index) for index in np.unique(assignment)]
    rows: list[dict[str, object]] = []
    for group in groups:
        positives = int(observed[group].sum())
        rate = posterior(positives, int(group.size), prior)
        rows.append(
            {
                "rows": int(group.size),
                "predicted_low": round(float(predicted[group].min()), 4),
                "predicted_high": round(float(predicted[group].max()), 4),
                "mean_predicted": round(float(predicted[group].mean()), 4),
                "positives": positives,
                "observed": rate.summary(level),
            }
        )
    return rows


__all__ = [
    "LooResult",
    "brier",
    "elpd_difference",
    "exact_elpd",
    "loo_probability",
    "psis_loo",
    "reliability_bins",
]
