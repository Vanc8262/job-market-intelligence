"""MCMC convergence diagnostics and the gate every sampled Phase 8 model must pass.

The statistics follow Vehtari, Gelman, Simpson, Carpenter, and Bürkner (2021),
"Rank-normalization, folding, and localization: an improved R-hat for assessing
convergence of MCMC", the same definitions Stan and ArviZ use:

* **R-hat** - the larger of the rank-normalized split R-hat of the draws and of their
  folded values (distance from the median), so scale problems are caught as well.
* **Bulk ESS** - effective sample size of the rank-normalized split chains.
* **Tail ESS** - the smaller effective sample size of the 5% and 95% quantile indicators.

They are implemented with NumPy and SciPy only, so the gate runs without the optional
PyMC/ArviZ stack; when ArviZ is installed the test suite checks the numbers against it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy import stats

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class DiagnosticThresholds:
    """Convergence requirements; the defaults are the Phase 8 gate."""

    max_rhat: float = 1.01
    min_ess_bulk: float = 400.0
    min_ess_tail: float = 400.0
    max_divergences: int = 0


@dataclass(frozen=True)
class ParameterDiagnostics:
    """Convergence statistics for one scalar parameter."""

    name: str
    rhat: float
    ess_bulk: float
    ess_tail: float

    def summary(self) -> dict[str, object]:
        """Return a JSON-safe row, rounded for reports."""
        return {
            "name": self.name,
            "rhat": round(self.rhat, 4),
            "ess_bulk": round(self.ess_bulk, 1),
            "ess_tail": round(self.ess_tail, 1),
        }


@dataclass(frozen=True)
class DiagnosticsReport:
    """Gate outcome for one model fit."""

    passed: bool
    parameters: list[ParameterDiagnostics]
    divergences: int
    thresholds: DiagnosticThresholds
    failures: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        """Return a JSON-safe report with the worst values first."""
        return {
            "passed": self.passed,
            "failures": self.failures,
            "divergences": self.divergences,
            "max_rhat": round(max((item.rhat for item in self.parameters), default=1.0), 4),
            "min_ess_bulk": round(min((item.ess_bulk for item in self.parameters), default=0.0), 1),
            "min_ess_tail": round(min((item.ess_tail for item in self.parameters), default=0.0), 1),
            "thresholds": {
                "max_rhat": self.thresholds.max_rhat,
                "min_ess_bulk": self.thresholds.min_ess_bulk,
                "min_ess_tail": self.thresholds.min_ess_tail,
                "max_divergences": self.thresholds.max_divergences,
            },
            "parameters": [item.summary() for item in self.parameters],
        }


def _as_chains(draws: Any) -> FloatArray:  # noqa: ANN401 - accepts any array-like
    values = np.asarray(draws, dtype=np.float64)
    if values.ndim == 1:
        values = values[np.newaxis, :]
    if values.ndim != 2:
        raise ValueError("draws must have shape (chains, draws)")
    if values.shape[1] < 4:
        raise ValueError("each chain needs at least 4 draws")
    if not np.all(np.isfinite(values)):
        raise ValueError("draws contain NaN or infinite values")
    return values


def _split(values: FloatArray) -> FloatArray:
    half = values.shape[1] // 2
    return np.concatenate([values[:, :half], values[:, -half:]], axis=0)


def _rank_normalize(values: FloatArray) -> FloatArray:
    ranks = stats.rankdata(values, method="average").reshape(values.shape)
    size = values.size
    return np.asarray(stats.norm.ppf((ranks - 0.375) / (size + 0.25)), dtype=np.float64)


def _basic_rhat(values: FloatArray) -> float:
    chains, draws = values.shape
    within = float(np.mean(np.var(values, axis=1, ddof=1)))
    if within == 0:
        return float("nan")
    between_over_n = float(np.var(np.mean(values, axis=1), ddof=1)) if chains > 1 else 0.0
    pooled = (draws - 1) / draws * within + between_over_n
    return float(np.sqrt(pooled / within))


def rhat(draws: Any) -> float:  # noqa: ANN401
    """Return the rank-normalized split R-hat (maximum of bulk and folded)."""
    split = _split(_as_chains(draws))
    bulk = _basic_rhat(_rank_normalize(split))
    folded = _basic_rhat(_rank_normalize(np.abs(split - np.median(split))))
    return float(np.nanmax([bulk, folded]))


def _autocovariance(values: FloatArray) -> FloatArray:
    draws = values.shape[1]
    centered = values - values.mean(axis=1, keepdims=True)
    size = 2 ** int(np.ceil(np.log2(2 * draws)))
    transform = np.fft.rfft(centered, n=size, axis=1)
    result = np.fft.irfft(transform * np.conjugate(transform), n=size, axis=1)[:, :draws]
    return np.asarray(result.real / draws, dtype=np.float64)


def _ess(values: FloatArray) -> float:
    """Effective sample size of (already split) chains, Geyer's initial monotone sequence."""
    chains, draws = values.shape
    acov = _autocovariance(values)
    chain_variance = acov[:, 0] * draws / (draws - 1)
    mean_variance = float(np.mean(chain_variance))
    if mean_variance == 0:
        return float(chains * draws)
    pooled = mean_variance * (draws - 1) / draws
    if chains > 1:
        pooled += float(np.var(np.mean(values, axis=1), ddof=1))
    mean_acov = np.mean(acov, axis=0)
    rho = np.zeros(draws)
    rho_even = 1.0
    rho[0] = rho_even
    rho_odd = 1.0 - (mean_variance - mean_acov[1]) / pooled
    rho[1] = rho_odd
    # Geyer's initial positive sequence: add autocorrelations in pairs while the
    # pair sum stays positive.
    lag = 1
    while lag < draws - 3 and rho_even + rho_odd > 0.0:
        rho_even = 1.0 - (mean_variance - mean_acov[lag + 1]) / pooled
        rho_odd = 1.0 - (mean_variance - mean_acov[lag + 2]) / pooled
        if rho_even + rho_odd >= 0:
            rho[lag + 1] = rho_even
            rho[lag + 2] = rho_odd
        lag += 2
    max_lag = lag - 2
    if rho_even > 0:  # use the last positive even term too
        rho[max_lag + 1] = rho_even
    # Geyer's initial monotone sequence: pair sums must not increase.
    lag = 1
    while lag <= max_lag - 2:
        if rho[lag + 1] + rho[lag + 2] > rho[lag - 1] + rho[lag]:
            rho[lag + 1] = (rho[lag - 1] + rho[lag]) / 2.0
            rho[lag + 2] = rho[lag + 1]
        lag += 2
    tau = (
        -1.0
        + 2.0 * float(np.sum(rho[: max_lag + 1]))
        + float(np.sum(rho[max_lag + 1 : max_lag + 2]))
    )
    tau = max(tau, 1.0 / np.log10(chains * draws))
    return float(chains * draws / tau)


def ess_bulk(draws: Any) -> float:  # noqa: ANN401
    """Return the bulk effective sample size."""
    return _ess(_rank_normalize(_split(_as_chains(draws))))


def ess_tail(draws: Any) -> float:  # noqa: ANN401
    """Return the tail effective sample size (5% and 95% quantile indicators)."""
    values = _as_chains(draws)
    sizes = []
    for probability in (0.05, 0.95):
        indicator = (values <= np.quantile(values, probability)).astype(np.float64)
        sizes.append(_ess(_split(indicator)))
    return float(min(sizes))


def _scalar_parameters(name: str, draws: Any) -> list[tuple[str, FloatArray]]:  # noqa: ANN401
    values = np.asarray(draws, dtype=np.float64)
    if values.ndim <= 2:
        return [(name, values)]
    flat = values.reshape(values.shape[0], values.shape[1], -1)
    shape = values.shape[2:]
    return [
        (
            f"{name}[{','.join(str(index) for index in np.unravel_index(position, shape))}]",
            flat[:, :, position],
        )
        for position in range(flat.shape[2])
    ]


def check_draws(
    draws: Mapping[str, Any],
    *,
    divergences: int = 0,
    thresholds: DiagnosticThresholds | None = None,
) -> DiagnosticsReport:
    """Run the gate on posterior draws keyed by parameter, each (chains, draws, ...)."""
    limits = thresholds or DiagnosticThresholds()
    parameters: list[ParameterDiagnostics] = []
    for name, values in draws.items():
        for label, scalar in _scalar_parameters(name, values):
            parameters.append(
                ParameterDiagnostics(
                    name=label,
                    rhat=rhat(scalar),
                    ess_bulk=ess_bulk(scalar),
                    ess_tail=ess_tail(scalar),
                )
            )
    failures = [
        f"{item.name}: R-hat {item.rhat:.3f} > {limits.max_rhat}"
        for item in parameters
        if not item.rhat <= limits.max_rhat
    ]
    failures += [
        f"{item.name}: bulk ESS {item.ess_bulk:.0f} < {limits.min_ess_bulk:g}"
        for item in parameters
        if item.ess_bulk < limits.min_ess_bulk
    ]
    failures += [
        f"{item.name}: tail ESS {item.ess_tail:.0f} < {limits.min_ess_tail:g}"
        for item in parameters
        if item.ess_tail < limits.min_ess_tail
    ]
    if divergences > limits.max_divergences:
        failures.append(f"{divergences} divergent transitions > {limits.max_divergences}")
    return DiagnosticsReport(
        passed=not failures,
        parameters=parameters,
        divergences=divergences,
        thresholds=limits,
        failures=failures,
    )


def check_inference_data(
    idata: Any,  # noqa: ANN401 - an ArviZ DataTree/InferenceData from the optional stack
    *,
    thresholds: DiagnosticThresholds | None = None,
) -> DiagnosticsReport:
    """Run the gate on a PyMC/ArviZ result (posterior group plus optional sample stats)."""
    posterior = idata["posterior"]
    draws = {
        str(name): np.asarray(posterior[name].values, dtype=np.float64)
        for name in posterior.data_vars
    }
    divergences = 0
    try:
        diverging = idata["sample_stats"]["diverging"]
        divergences = int(np.asarray(diverging.values).sum())
    except (KeyError, AttributeError, TypeError):
        divergences = 0
    return check_draws(draws, divergences=divergences, thresholds=thresholds)


__all__ = [
    "DiagnosticThresholds",
    "DiagnosticsReport",
    "ParameterDiagnostics",
    "check_draws",
    "check_inference_data",
    "ess_bulk",
    "ess_tail",
    "rhat",
]
