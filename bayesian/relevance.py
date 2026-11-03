"""B3: relevance weights learned from tracking decisions (Bayesian logistic regression).

The relevance score is a weighted average of four signals in [0, 1], so B3 keeps that
shape and learns the weights on the simplex (design: docs/design/phase-8-b3-...md)::

    w     ~ Dirichlet(prior_strength * w0)        # w0: the configured weights, summing to 1
    tau   ~ LogNormal(log(median), sigma)
    alpha ~ Normal(0, intercept_sigma)
    logit P(keep_i) = alpha + tau * (s_i . w - mean_i(s_i . w0))

Four models are compared with PSIS-LOO: ``baseline`` (alpha only), ``current_score``
(w fixed at w0), ``learned_weights`` (the model above), and ``weights_plus_fit`` (plus a
coefficient on the Phase 6.3 fit score). Every fit must pass the B0 diagnostics gate or it
is reported as failed with no posterior numbers.

Only ``pymc_sampler`` touches PyMC (the optional ``bayes`` extra), and LOO needs ArviZ;
everything else is NumPy, so the report logic can be tested with any sampler that returns
draws. The output is advisory: it suggests weights, and changing ``weights:`` in the resume
profile stays a human edit.
"""

from __future__ import annotations

import math
import warnings
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
from numpy.typing import NDArray

from bayesian.diagnostics import DiagnosticThresholds, check_draws
from bayesian.evaluation import (
    LooResult,
    brier,
    elpd_difference,
    exact_elpd,
    loo_probability,
    psis_loo,
    reliability_bins,
)
from bayesian.records import model_card
from utils.config import BayesianRelevanceWeightsConfig

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]

SIGNALS = ("skills", "title", "experience", "location")
BASELINE = "baseline"
CURRENT_SCORE = "current_score"
LEARNED_WEIGHTS = "learned_weights"
WEIGHTS_PLUS_FIT = "weights_plus_fit"
MODEL_QUESTIONS = {
    BASELINE: "Base rate only: is any signal better than the share of postings you keep?",
    CURRENT_SCORE: "Is today's hand-set relevance score informative and calibrated?",
    LEARNED_WEIGHTS: "Do learned weights predict your decisions better than the current ones?",
    WEIGHTS_PLUS_FIT: "Does 6.3 application fit add information beyond relevance?",
}
# Smallest Dirichlet pseudo-count, so a weight configured as 0 still has a proper prior.
PRIOR_FLOOR = 0.5
# Odds multiplier reported for a score gap of this many points (out of 100).
SCORE_GAP = 0.25
# Posterior draws used for per-posting probabilities (thinned evenly).
POSTING_DRAWS = 1000


@dataclass(frozen=True)
class TrainingSet:
    """B3 training rows as arrays, with what was left out and why."""

    job_ids: tuple[str, ...]
    signals: FloatArray
    labels: IntArray
    # The 6.3 fit score on a 0-1 scale; NaN where the posting had no comparison.
    fit: FloatArray
    sources: tuple[str, ...]
    excluded: dict[str, int] = field(default_factory=dict)
    resume_versions: dict[str, int] = field(default_factory=dict)
    comparison_versions: dict[str, int] = field(default_factory=dict)

    @property
    def size(self) -> int:
        """Number of training rows."""
        return len(self.job_ids)

    @property
    def positives(self) -> int:
        """Rows labeled positive (kept)."""
        return int(self.labels.sum())

    @property
    def negatives(self) -> int:
        """Rows labeled negative (archived)."""
        return self.size - self.positives

    def subset(self, mask: Sequence[bool] | NDArray[np.bool_]) -> TrainingSet:
        """Return the rows where ``mask`` is true (exclusion counts are kept)."""
        keep = np.asarray(mask, dtype=bool)
        return replace(
            self,
            job_ids=tuple(job for job, kept in zip(self.job_ids, keep, strict=True) if kept),
            signals=self.signals[keep],
            labels=self.labels[keep],
            fit=self.fit[keep],
            sources=tuple(src for src, kept in zip(self.sources, keep, strict=True) if kept),
        )

    def without(self, row: int) -> TrainingSet:
        """Return the rows without one row (for an exact leave-one-out refit)."""
        mask = np.ones(self.size, dtype=bool)
        mask[row] = False
        return self.subset(mask)


@dataclass(frozen=True)
class FitInputs:
    """Everything one model fit needs, as arrays."""

    signals: FloatArray
    labels: IntArray
    fit: FloatArray
    prior_center: FloatArray
    prior_strength: float
    center_score: float
    center_fit: float


@dataclass(frozen=True)
class SampledFit:
    """Posterior draws of one fit, each (chains, draws, ...), and its divergence count."""

    draws: dict[str, FloatArray]
    divergences: int = 0


type Sampler = Callable[[str, FitInputs, BayesianRelevanceWeightsConfig], SampledFit]


def training_set(rows: Sequence[Mapping[str, object]], *, include_sync: bool) -> TrainingSet:
    """Turn ``training_rows()`` output into arrays, dropping rows that lack signals."""
    excluded: Counter[str] = Counter()
    job_ids: list[str] = []
    signals: list[list[float]] = []
    labels: list[int] = []
    fits: list[float] = []
    sources: list[str] = []
    resume_versions: Counter[str] = Counter()
    comparison_versions: Counter[str] = Counter()
    for row in rows:
        source = str(row.get("source") or "tracking")
        if not row.get("captured", False):
            excluded["not_captured"] += 1
            continue
        if source == "sync" and not include_sync:
            excluded["sync_rows"] += 1
            continue
        values = [_number(row.get(f"{name}_signal")) for name in SIGNALS]
        if any(value is None for value in values):
            excluded["missing_signal"] += 1
            continue
        job_ids.append(str(row["job_id"]))
        signals.append([min(1.0, max(0.0, float(value or 0.0))) for value in values])
        labels.append(1 if row.get("label") == 1 else 0)
        fit = _number(row.get("fit_score"))
        fits.append(min(1.0, max(0.0, fit / 100.0)) if fit is not None else math.nan)
        sources.append(source)
        resume_versions[str(row.get("resume_version_id") or "unknown")] += 1
        comparison_versions[str(row.get("comparison_model_version") or "unknown")] += 1
    return TrainingSet(
        job_ids=tuple(job_ids),
        signals=np.asarray(signals, dtype=np.float64).reshape(-1, len(SIGNALS)),
        labels=np.asarray(labels, dtype=np.int64),
        fit=np.asarray(fits, dtype=np.float64),
        sources=tuple(sources),
        excluded=dict(excluded),
        resume_versions=dict(resume_versions),
        comparison_versions=dict(comparison_versions),
    )


def prior_center(weights: Mapping[str, float]) -> FloatArray:
    """Return the configured weights as a point on the simplex, in ``SIGNALS`` order."""
    values = np.asarray([float(weights.get(name, 0.0)) for name in SIGNALS], dtype=np.float64)
    total = float(values.sum())
    if total <= 0 or (values < 0).any():
        raise ValueError("relevance weights must be non-negative with a positive total")
    return np.asarray(values / total, dtype=np.float64)


def dirichlet_concentration(center: FloatArray, strength: float) -> FloatArray:
    """Return Dirichlet pseudo-counts ``strength * center``, floored at ``PRIOR_FLOOR``."""
    return np.asarray(np.maximum(center * strength, PRIOR_FLOOR), dtype=np.float64)


def linear_predictor(
    model: str,
    draws: Mapping[str, FloatArray],
    inputs: FitInputs,
    signals: FloatArray,
    fit: FloatArray,
) -> FloatArray:
    """Return logit P(keep) for each draw and row: shape (chains, draws, rows)."""
    alpha = np.asarray(draws["alpha"], dtype=np.float64)[..., None]
    eta = np.broadcast_to(alpha, (*alpha.shape[:-1], signals.shape[0])).copy()
    if model == BASELINE:
        return eta
    tau = np.asarray(draws["tau"], dtype=np.float64)[..., None]
    if model == CURRENT_SCORE:
        score = (signals @ inputs.prior_center)[None, None, :]
    else:
        score = np.einsum("cdk,nk->cdn", np.asarray(draws["weights"], dtype=np.float64), signals)
    eta = eta + tau * (score - inputs.center_score)
    if model == WEIGHTS_PLUS_FIT:
        gamma = np.asarray(draws["gamma"], dtype=np.float64)[..., None]
        eta = eta + gamma * (fit[None, None, :] - inputs.center_fit)
    return np.asarray(eta, dtype=np.float64)


def log_likelihood(eta: FloatArray, labels: IntArray) -> FloatArray:
    """Return the Bernoulli log-likelihood of each label under each draw's logit."""
    observed = labels.astype(np.float64)[None, None, :]
    # log p = -log(1 + e^-eta), log(1 - p) = -log(1 + e^eta), computed stably.
    return np.asarray(
        -(observed * np.logaddexp(0.0, -eta) + (1.0 - observed) * np.logaddexp(0.0, eta)),
        dtype=np.float64,
    )


def pymc_sampler(
    model: str, inputs: FitInputs, settings: BayesianRelevanceWeightsConfig
) -> SampledFit:
    """Fit one model with PyMC (nutpie or PyMC's own NUTS) and return its draws."""
    import pymc as pm  # noqa: PLC0415 - optional `bayes` extra

    sampler = settings.sampler
    with pm.Model():
        alpha = pm.Normal("alpha", mu=0.0, sigma=settings.intercept_sigma)
        eta: Any = alpha
        if model != BASELINE:
            tau = pm.LogNormal(
                "tau", mu=math.log(settings.tau_prior.median), sigma=settings.tau_prior.sigma
            )
            if model == CURRENT_SCORE:
                score: Any = inputs.signals @ inputs.prior_center
            else:
                weights = pm.Dirichlet(
                    "weights",
                    a=dirichlet_concentration(inputs.prior_center, inputs.prior_strength),
                )
                score = pm.math.dot(inputs.signals, weights)
            eta = eta + tau * (score - inputs.center_score)
        if model == WEIGHTS_PLUS_FIT:
            gamma = pm.Normal("gamma", mu=0.0, sigma=settings.fit_coefficient_sigma)
            eta = eta + gamma * (inputs.fit - inputs.center_fit)
        pm.Bernoulli("y", logit_p=eta, observed=inputs.labels)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            idata = pm.sample(
                draws=sampler.draws,
                tune=sampler.tune,
                chains=sampler.chains,
                nuts_sampler=sampler.name,
                random_seed=sampler.seed,
                target_accept=sampler.target_accept,
                progressbar=False,
                compute_convergence_checks=False,
            )
    posterior = idata["posterior"]
    draws = {
        str(name): np.asarray(posterior[name].values, dtype=np.float64)
        for name in posterior.data_vars
    }
    try:
        divergences = int(np.asarray(idata["sample_stats"]["diverging"].values).sum())
    except (KeyError, AttributeError, TypeError):
        divergences = 0
    return SampledFit(draws=draws, divergences=divergences)


@dataclass
class ModelFit:
    """One fitted model: its draws, gate outcome, and LOO results when it passed."""

    name: str
    rows: TrainingSet
    inputs: FitInputs
    sampled: SampledFit
    diagnostics: dict[str, object]
    passed: bool
    loo: LooResult | None = None
    loo_probabilities: FloatArray | None = None


@dataclass
class RelevanceFit:
    """The B3 report plus the draws of every model that passed, for saving."""

    report: dict[str, object]
    draws: dict[str, dict[str, FloatArray]] = field(default_factory=dict)
    postings: list[dict[str, object]] = field(default_factory=list)


def fit_relevance_weights(
    rows: Sequence[Mapping[str, object]],
    *,
    weights: Mapping[str, float],
    settings: BayesianRelevanceWeightsConfig,
    thresholds: DiagnosticThresholds,
    credible_level: float = 0.9,
    minimum_labels: int = 30,
    minimum_each: int = 3,
    sampler: Sampler | None = None,
    postings: Sequence[tuple[str, Sequence[float]]] = (),
) -> RelevanceFit:
    """Fit B3 on training rows and return the advisory report (never changes config)."""
    run = sampler or pymc_sampler
    center = prior_center(weights)
    data = training_set(rows, include_sync=settings.include_sync_rows)
    report: dict[str, object] = {
        "model_card": model_card(
            "relevance_weights",
            stage="B3",
            settings=settings,
            replaces="Hand-set relevance weights; simulator heuristic",
            version=settings.version,
        ),
        "signals": list(SIGNALS),
        "current_weights": _weights_dict(center),
        "credible_level": credible_level,
        "data": _data_summary(data, center),
        "prior": {
            "strength": settings.prior_strength,
            "concentration": _weights_dict(
                dirichlet_concentration(center, settings.prior_strength), scale=1.0, digits=3
            ),
            "floor_applied": bool((center * settings.prior_strength < PRIOR_FLOOR).any()),
        },
        "biases": [
            "Only postings you decided on are labeled, mostly from the top of the ranking, so "
            "the weights describe what separates kept from archived among those, not all "
            "postings.",
            "Rows added later by sync carry the signals of the day they were added.",
            "'Reviewing' counts as a positive, a softer yes than saving or applying.",
        ],
    }
    if data.size < minimum_labels or min(data.positives, data.negatives) < minimum_each:
        report["status"] = "insufficient_data"
        report["message"] = (
            f"B3 needs at least {minimum_labels} usable labels with {minimum_each} of each kind; "
            f"{data.size} usable ({data.positives} positive, {data.negatives} negative)."
        )
        return RelevanceFit(report=report)

    center_score = float((data.signals @ center).mean())
    report["prior_predictive"] = prior_predictive(
        data, center, settings, center_score, credible_level
    )
    fits: dict[str, ModelFit] = {}
    model_rows: dict[str, dict[str, object]] = {}
    for name in settings.models:
        rows_for_model = data
        if name == WEIGHTS_PLUS_FIT:
            rows_for_model = data.subset(~np.isnan(data.fit))
            if min(rows_for_model.positives, rows_for_model.negatives) < minimum_each:
                model_rows[name] = {
                    "question": MODEL_QUESTIONS[name],
                    "status": "skipped",
                    "reason": "Too few labeled postings have a 6.3 fit score.",
                    "rows": rows_for_model.size,
                }
                continue
        fitted = _fit_model(
            name,
            rows_for_model,
            center,
            settings.prior_strength,
            center_score,
            settings,
            thresholds,
            run,
            credible_level,
        )
        fits[name] = fitted
        model_rows[name] = _model_summary(fitted, settings, credible_level)
    report["models"] = model_rows
    report["comparisons"] = _comparisons(
        fits, data, center, center_score, settings, thresholds, run, credible_level
    )

    learned = fits.get(LEARNED_WEIGHTS)
    improves = _learned_beats_current(report["comparisons"], settings)
    if learned is not None and learned.passed:
        summary = weight_summary(
            learned.sampled.draws["weights"], center, credible_level, settings, improves=improves
        )
        summary["labels_likely_needed"] = labels_needed(summary, data.size, settings)
        report["weights"] = summary
        report["status"] = summary["verdict"]
    else:
        report["weights"] = None
        report["status"] = "not_reported"
        report["message"] = "The learned-weights model did not pass the diagnostics gate."
    report["sensitivity"] = _sensitivity(
        data, center, center_score, settings, thresholds, run, credible_level, minimum_each
    )

    result = RelevanceFit(report=report)
    for model_name, fitted in fits.items():
        if fitted.passed:
            result.draws[model_name] = fitted.sampled.draws
    if learned is not None and learned.passed and postings:
        scores = data.signals @ center
        result.postings = posting_probabilities(
            learned.sampled.draws,
            learned.inputs,
            postings,
            training_range=(float(scores.min()), float(scores.max())),
            level=credible_level,
        )
    return result


def prior_predictive(
    data: TrainingSet,
    center: FloatArray,
    settings: BayesianRelevanceWeightsConfig,
    center_score: float,
    level: float,
    draws: int = 4000,
) -> dict[str, object]:
    """Describe what the learned-weights prior implies before seeing any label."""
    rng = np.random.default_rng(settings.sampler.seed)
    weights = rng.dirichlet(dirichlet_concentration(center, settings.prior_strength), size=draws)
    tau = rng.lognormal(math.log(settings.tau_prior.median), settings.tau_prior.sigma, size=draws)
    alpha = rng.normal(0.0, settings.intercept_sigma, size=draws)
    eta = alpha[:, None] + tau[:, None] * (weights @ data.signals.T - center_score)
    probability = 1.0 / (1.0 + np.exp(-eta))
    extreme = float(((probability < 0.05) | (probability > 0.95)).mean())
    return {
        "probability": _interval(probability.reshape(-1), level),
        "extreme_share": round(extreme, 4),
        "odds_multiplier_per_25_points": _median_interval(np.exp(tau * SCORE_GAP), level),
        "weights": {
            name: _interval(weights[:, index] * 100, level, digits=2)
            for index, name in enumerate(SIGNALS)
        },
        "passed": extreme < 0.5,
        "note": "Shown, not enforced: the prior should not put most mass near 0 or 1.",
    }


def weight_summary(
    weight_draws: FloatArray,
    center: FloatArray,
    level: float,
    settings: BayesianRelevanceWeightsConfig,
    *,
    improves: bool,
) -> dict[str, object]:
    """Summarize the posterior weights against the current ones, with verdicts."""
    flat = np.asarray(weight_draws, dtype=np.float64).reshape(-1, len(SIGNALS))
    probability = settings.recommend.direction_probability
    rows: dict[str, dict[str, object]] = {}
    for index, name in enumerate(SIGNALS):
        values = flat[:, index] * 100
        above = float((flat[:, index] > center[index]).mean())
        if not improves:
            verdict = "keep"
        elif above >= probability:
            verdict = "raise"
        elif above <= 1 - probability:
            verdict = "lower"
        else:
            verdict = "keep"
        rows[name] = {
            "current": round(float(center[index] * 100), 2),
            **_interval(values, level, digits=2),
            "probability_above_current": round(above, 4),
            "verdict": verdict,
        }
    suggested = largest_remainder(flat.mean(axis=0) * 100)
    changed = any(row["verdict"] != "keep" for row in rows.values())
    return {
        "by_signal": rows,
        "suggested": dict(zip(SIGNALS, suggested, strict=True)),
        "learned_beats_current": improves,
        "verdict": "change_suggested" if changed else "keep_current",
        "explanation": (
            "Weights move only when a direction is at least "
            f"{probability:.0%} probable and learned weights predict your decisions better than "
            f"the current score by more than {settings.recommend.elpd_se_multiple:g} standard "
            "errors (PSIS-LOO)."
        ),
    }


def labels_needed(
    summary: Mapping[str, object], size: int, settings: BayesianRelevanceWeightsConfig
) -> dict[str, object]:
    """Roughly project the labels needed for every weight interval to reach the target width.

    Interval width shrinks about as 1/sqrt(n); the prior also narrows intervals, so this is
    an order of magnitude, not a promise.
    """
    target = settings.recommend.target_interval_points
    rows = summary.get("by_signal")
    widths = [
        float(row["high"]) - float(row["low"])
        for row in (rows.values() if isinstance(rows, dict) else [])
    ]
    widest = max(widths, default=0.0)
    needed = size if widest <= target else math.ceil(size * (widest / target) ** 2)
    return {
        "target_interval_points": target,
        "widest_interval_points": round(widest, 2),
        "labels": needed,
        "note": "Rough: interval width shrinks about as 1/sqrt(labels).",
    }


def largest_remainder(values: Sequence[float] | FloatArray, total: int = 100) -> list[int]:
    """Round non-negative values to integers that sum to ``total``, keeping proportions."""
    array = np.asarray(values, dtype=np.float64)
    scaled = array / array.sum() * total
    floors = np.floor(scaled).astype(int)
    order = np.argsort(-(scaled - floors), kind="stable")
    for index in order[: total - int(floors.sum())]:
        floors[index] += 1
    return [int(value) for value in floors]


def posting_probabilities(
    draws: Mapping[str, FloatArray],
    inputs: FitInputs,
    postings: Sequence[tuple[str, Sequence[float]]],
    *,
    training_range: tuple[float, float],
    level: float,
) -> list[dict[str, object]]:
    """Return P(keep) with its interval for each posting under the learned weights."""
    if not postings:
        return []
    chains, count = np.asarray(draws["alpha"]).shape[:2]
    step = max(1, (chains * count) // POSTING_DRAWS)
    thinned = {
        name: np.asarray(value, dtype=np.float64).reshape(chains * count, *np.shape(value)[2:])[
            ::step
        ][None]
        for name, value in draws.items()
    }
    signals = np.asarray([list(values) for _, values in postings], dtype=np.float64)
    eta = linear_predictor(
        LEARNED_WEIGHTS, thinned, inputs, signals, np.full(signals.shape[0], math.nan)
    )[0]
    probability = 1.0 / (1.0 + np.exp(-eta))
    tail = (1 - level) / 2
    low, high = np.quantile(probability, [tail, 1 - tail], axis=0)
    current = signals @ inputs.prior_center
    lowest, highest = training_range
    return [
        {
            "job_id": job_id,
            "mean": round(float(probability[:, index].mean()), 4),
            "low": round(float(low[index]), 4),
            "high": round(float(high[index]), 4),
            "extrapolated": bool(current[index] < lowest or current[index] > highest),
        }
        for index, (job_id, _) in enumerate(postings)
    ]


def _fit_model(
    name: str,
    data: TrainingSet,
    center: FloatArray,
    strength: float,
    center_score: float,
    settings: BayesianRelevanceWeightsConfig,
    thresholds: DiagnosticThresholds,
    run: Sampler,
    level: float,
    *,
    loo: bool = True,
) -> ModelFit:
    fit_values = data.fit[~np.isnan(data.fit)]
    inputs = FitInputs(
        signals=data.signals,
        labels=data.labels,
        fit=np.nan_to_num(data.fit, nan=0.0),
        prior_center=center,
        prior_strength=strength,
        center_score=center_score,
        center_fit=float(fit_values.mean()) if fit_values.size else 0.0,
    )
    sampled = run(name, inputs, settings)
    gate = check_draws(sampled.draws, divergences=sampled.divergences, thresholds=thresholds)
    fitted = ModelFit(
        name=name,
        rows=data,
        inputs=inputs,
        sampled=sampled,
        diagnostics=gate.summary(),
        passed=gate.passed,
    )
    if not gate.passed or not loo:
        return fitted
    eta = linear_predictor(name, sampled.draws, inputs, inputs.signals, inputs.fit)
    result = psis_loo(log_likelihood(eta, inputs.labels))
    for row in result.high_k_rows(settings.pareto_k_threshold)[: settings.max_exact_refits]:
        exact = _exact_refit(
            name, data, row, center, strength, center_score, settings, thresholds, run, level
        )
        if exact is not None:
            result = result.with_exact(row, exact)
    fitted.loo = result
    fitted.loo_probabilities = loo_probability(result, inputs.labels.tolist())
    return fitted


def _exact_refit(
    name: str,
    data: TrainingSet,
    row: int,
    center: FloatArray,
    strength: float,
    center_score: float,
    settings: BayesianRelevanceWeightsConfig,
    thresholds: DiagnosticThresholds,
    run: Sampler,
    level: float,
) -> float | None:
    """Refit without one row and return that row's exact log predictive density."""
    refit = _fit_model(
        name,
        data.without(row),
        center,
        strength,
        center_score,
        settings,
        thresholds,
        run,
        level,
        loo=False,
    )
    if not refit.passed:
        return None
    held_signals = data.signals[row : row + 1]
    held_fit = np.nan_to_num(data.fit[row : row + 1], nan=0.0)
    eta = linear_predictor(name, refit.sampled.draws, refit.inputs, held_signals, held_fit)
    return exact_elpd(log_likelihood(eta, data.labels[row : row + 1]))


def _model_summary(
    fitted: ModelFit, settings: BayesianRelevanceWeightsConfig, level: float
) -> dict[str, object]:
    summary: dict[str, object] = {
        "question": MODEL_QUESTIONS[fitted.name],
        "rows": fitted.rows.size,
        "diagnostics": fitted.diagnostics,
        "status": "passed" if fitted.passed else "failed_diagnostics",
    }
    if not fitted.passed or fitted.loo is None or fitted.loo_probabilities is None:
        return summary
    labels = fitted.rows.labels
    eta = linear_predictor(
        fitted.name, fitted.sampled.draws, fitted.inputs, fitted.inputs.signals, fitted.inputs.fit
    )
    probability = 1.0 / (1.0 + np.exp(-eta))
    summary["parameters"] = {
        name: _interval(np.asarray(values), level)
        for name, values in fitted.sampled.draws.items()
        if name != "weights"
    }
    summary["loo"] = fitted.loo.summary(settings.pareto_k_threshold)
    summary["unreliable_rows"] = [
        fitted.rows.job_ids[row] for row in fitted.loo.high_k_rows(settings.pareto_k_threshold)
    ]
    summary["brier"] = brier(fitted.loo_probabilities, labels.tolist())
    # The baseline predicts one rate, so its LOO probabilities differ only by each row's own
    # label; binning them would show an artifact, not calibration.
    if fitted.name != BASELINE:
        summary["reliability"] = reliability_bins(
            fitted.loo_probabilities, labels.tolist(), bins=settings.calibration_bins, level=level
        )
    summary["posterior_predictive"] = _positives_check(
        probability, labels, settings.sampler.seed, level
    )
    return summary


def _comparisons(
    fits: Mapping[str, ModelFit],
    data: TrainingSet,
    center: FloatArray,
    center_score: float,
    settings: BayesianRelevanceWeightsConfig,
    thresholds: DiagnosticThresholds,
    run: Sampler,
    level: float,
) -> list[dict[str, object]]:
    """Paired ELPD differences between models fitted on the same rows."""
    pairs = [
        (CURRENT_SCORE, BASELINE),
        (LEARNED_WEIGHTS, BASELINE),
        (LEARNED_WEIGHTS, CURRENT_SCORE),
    ]
    rows: list[dict[str, object]] = []
    for better, other in pairs:
        row = _difference(fits.get(better), fits.get(other))
        if row is not None:
            rows.append(row)
    with_fit = fits.get(WEIGHTS_PLUS_FIT)
    learned = fits.get(LEARNED_WEIGHTS)
    if with_fit is not None and learned is not None:
        same_rows = learned
        if with_fit.rows.size != data.size:
            same_rows = _fit_model(
                LEARNED_WEIGHTS,
                with_fit.rows,
                center,
                settings.prior_strength,
                center_score,
                settings,
                thresholds,
                run,
                level,
            )
        row = _difference(with_fit, same_rows)
        if row is not None:
            row["rows"] = with_fit.rows.size
            rows.append(row)
    return rows


def _difference(better: ModelFit | None, other: ModelFit | None) -> dict[str, object] | None:
    if better is None or other is None:
        return None
    row: dict[str, object] = {"model": better.name, "against": other.name}
    if better.loo is None or other.loo is None:
        row["status"] = "not_compared"
        row["reason"] = "A model in this pair did not pass the diagnostics gate."
        return row
    row.update(elpd_difference(better.loo, other.loo))
    row["status"] = "compared"
    return row


def _learned_beats_current(comparisons: object, settings: BayesianRelevanceWeightsConfig) -> bool:
    for row in comparisons if isinstance(comparisons, list) else []:
        if row.get("model") == LEARNED_WEIGHTS and row.get("against") == CURRENT_SCORE:
            if row.get("status") != "compared":
                return False
            return float(row["difference"]) > settings.recommend.elpd_se_multiple * float(row["se"])
    return False


def _sensitivity(
    data: TrainingSet,
    center: FloatArray,
    center_score: float,
    settings: BayesianRelevanceWeightsConfig,
    thresholds: DiagnosticThresholds,
    run: Sampler,
    level: float,
    minimum_each: int,
) -> list[dict[str, object]]:
    """Learned-weights fits under other choices, so the prior's pull can be seen."""
    variants: list[tuple[str, TrainingSet, float]] = [
        (f"prior_strength_{value:g}", data, value) for value in settings.sensitivity.prior_strengths
    ]
    sync = np.asarray([source == "sync" for source in data.sources], dtype=bool)
    if settings.sensitivity.without_sync and sync.any():
        variants.append(("without_sync", data.subset(~sync), settings.prior_strength))
    rows: list[dict[str, object]] = []
    for label, rows_for_fit, strength in variants:
        row: dict[str, object] = {
            "variant": label,
            "rows": rows_for_fit.size,
            "prior_strength": strength,
        }
        if min(rows_for_fit.positives, rows_for_fit.negatives) < minimum_each:
            row["status"] = "skipped"
            rows.append(row)
            continue
        fitted = _fit_model(
            LEARNED_WEIGHTS,
            rows_for_fit,
            center,
            strength,
            center_score,
            settings,
            thresholds,
            run,
            level,
            loo=False,
        )
        row["status"] = "passed" if fitted.passed else "failed_diagnostics"
        row["diagnostics"] = {
            key: value for key, value in fitted.diagnostics.items() if key != "parameters"
        }
        if fitted.passed:
            flat = fitted.sampled.draws["weights"].reshape(-1, len(SIGNALS)) * 100
            row["weights"] = {
                name: _interval(flat[:, index], level, digits=2)
                for index, name in enumerate(SIGNALS)
            }
        rows.append(row)
    return rows


def _positives_check(
    probability: FloatArray, labels: IntArray, seed: int, level: float
) -> dict[str, object]:
    """Posterior predictive check on the number of kept postings."""
    rng = np.random.default_rng(seed)
    flat = probability.reshape(-1, probability.shape[-1])
    replicated = (rng.random(flat.shape) < flat).sum(axis=1)
    observed = int(labels.sum())
    return {
        "observed_positives": observed,
        "replicated_positives": _interval(replicated.astype(np.float64), level, digits=1),
        "p_value": round(float((replicated >= observed).mean()), 4),
    }


def _data_summary(data: TrainingSet, center: FloatArray) -> dict[str, object]:
    scores = data.signals @ center * 100 if data.size else np.zeros(0)
    return {
        "rows": data.size,
        "positive": data.positives,
        "negative": data.negatives,
        "excluded": data.excluded,
        "sync_rows": sum(source == "sync" for source in data.sources),
        "rows_with_fit": int((~np.isnan(data.fit)).sum()),
        "current_score_range": (
            [round(float(scores.min()), 2), round(float(scores.max()), 2)] if data.size else None
        ),
        "resume_versions": data.resume_versions,
        "comparison_versions": data.comparison_versions,
    }


def _interval(values: FloatArray, level: float, digits: int = 4) -> dict[str, float]:
    flat = np.asarray(values, dtype=np.float64).reshape(-1)
    tail = (1 - level) / 2
    low, high = np.quantile(flat, [tail, 1 - tail])
    return {
        "mean": round(float(flat.mean()), digits),
        "low": round(float(low), digits),
        "high": round(float(high), digits),
    }


def _median_interval(values: FloatArray, level: float) -> dict[str, float]:
    tail = (1 - level) / 2
    low, median, high = np.quantile(values, [tail, 0.5, 1 - tail])
    return {
        "median": round(float(median), 3),
        "low": round(float(low), 3),
        "high": round(float(high), 3),
    }


def _weights_dict(values: FloatArray, *, scale: float = 100.0, digits: int = 2) -> dict[str, float]:
    return {name: round(float(values[index] * scale), digits) for index, name in enumerate(SIGNALS)}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(float(value)) else None


__all__ = [
    "BASELINE",
    "CURRENT_SCORE",
    "LEARNED_WEIGHTS",
    "SIGNALS",
    "WEIGHTS_PLUS_FIT",
    "FitInputs",
    "RelevanceFit",
    "SampledFit",
    "Sampler",
    "TrainingSet",
    "dirichlet_concentration",
    "fit_relevance_weights",
    "labels_needed",
    "largest_remainder",
    "linear_predictor",
    "log_likelihood",
    "posting_probabilities",
    "prior_center",
    "prior_predictive",
    "pymc_sampler",
    "training_set",
    "weight_summary",
]
