"""Phase 8 B3: relevance weights from tracking decisions (synthetic rows only)."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

import numpy as np
import pytest

from analytics.relevance_model import (
    mart_postings,
    read_relevance_report,
    relevance_model_dir,
    run_relevance_model,
)
from analytics.tracking_labels import (
    TrackingClass,
    TrackingLabel,
    TrackingLabelStore,
    TrackingSource,
    tracking_labels_path,
)
from bayesian.diagnostics import DiagnosticThresholds
from bayesian.evaluation import (
    LooResult,
    brier,
    elpd_difference,
    exact_elpd,
    loo_probability,
    reliability_bins,
)
from bayesian.relevance import (
    SIGNALS,
    FitInputs,
    SampledFit,
    dirichlet_concentration,
    fit_relevance_weights,
    labels_needed,
    largest_remainder,
    linear_predictor,
    log_likelihood,
    prior_center,
    training_set,
    weight_summary,
)
from dashboard.app import DashboardHTTPServer, create_server
from utils.config import BayesianRelevanceWeightsConfig, Config

WEIGHTS = {"skills": 35, "title": 30, "experience": 20, "location": 15}
START = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _rows(
    count: int, *, true_weights: tuple[float, ...] = (0.35, 0.30, 0.20, 0.15), seed: int = 7
) -> list[dict[str, object]]:
    """Synthetic training rows drawn from a known logistic model."""
    rng = np.random.default_rng(seed)
    weights = np.asarray(true_weights)
    rows: list[dict[str, object]] = []
    for index in range(count):
        signals = rng.uniform(0, 1, len(SIGNALS))
        probability = 1 / (1 + math.exp(-(0.5 + 8 * (float(signals @ weights) - 0.5))))
        rows.append(
            {
                "job_id": f"job-{index}",
                "label": int(rng.random() < probability),
                "source": "sync" if index % 6 == 0 else "tracking",
                "captured": True,
                "fit_score": float(rng.uniform(30, 90)) if index % 5 else None,
                "resume_version_id": "resume-v1",
                "comparison_model_version": "1.5.8",
                **{
                    f"{name}_signal": float(value)
                    for name, value in zip(SIGNALS, signals, strict=True)
                },
            }
        )
    return rows


def _iid_sampler(seed: int = 3, chains: int = 4, draws: int = 600):  # type: ignore[no-untyped-def]
    """A stand-in for PyMC: independent draws near plausible values, so the gate passes."""

    def sample(
        model: str, inputs: FitInputs, settings: BayesianRelevanceWeightsConfig
    ) -> SampledFit:
        rng = np.random.default_rng(seed)
        shape = (chains, draws)
        result = {"alpha": rng.normal(0.6, 0.3, shape)}
        if model != "baseline":
            result["tau"] = rng.lognormal(math.log(5), 0.3, shape)
        if model in {"learned_weights", "weights_plus_fit"}:
            concentration = dirichlet_concentration(inputs.prior_center, inputs.prior_strength)
            result["weights"] = rng.dirichlet(concentration, size=shape)
        if model == "weights_plus_fit":
            result["gamma"] = rng.normal(0.0, 0.5, shape)
        return SampledFit(draws=result)

    return sample


def _stuck_sampler(
    model: str, inputs: FitInputs, settings: BayesianRelevanceWeightsConfig
) -> SampledFit:
    """Chains that never mix: every one sits at a different value."""
    offsets = np.arange(4, dtype=np.float64)[:, None]
    shape = (4, 200)
    noise = np.random.default_rng(5).normal(0, 0.01, shape)
    result = {"alpha": noise + offsets}
    if model != "baseline":
        result["tau"] = 1 + noise + offsets
    if model in {"learned_weights", "weights_plus_fit"}:
        result["weights"] = np.broadcast_to(inputs.prior_center, (*shape, 4)).copy()
        result["weights"] += noise[..., None] * 0.01
        result["weights"][..., 0] += offsets * 0.01
    if model == "weights_plus_fit":
        result["gamma"] = noise + offsets
    return SampledFit(draws=result, divergences=3)


def _settings(**changes: object) -> BayesianRelevanceWeightsConfig:
    return BayesianRelevanceWeightsConfig.model_validate(
        {**BayesianRelevanceWeightsConfig().model_dump(), **changes}
    )


# --- pure NumPy pieces -------------------------------------------------------------------


def test_training_set_keeps_rows_with_all_signals_and_scales_fit() -> None:
    rows = _rows(4)
    rows.append({"job_id": "gone", "label": 1, "captured": False})
    rows.append({"job_id": "partial", "label": 0, "captured": True, "skills_signal": 0.5})

    data = training_set(rows, include_sync=True)

    assert data.size == 4
    assert data.excluded == {"not_captured": 1, "missing_signal": 1}
    assert data.signals.shape == (4, 4)
    assert np.isnan(data.fit[0])  # job-0 has no fit score
    assert 0.3 <= data.fit[1] <= 0.9
    assert data.resume_versions == {"resume-v1": 4}
    without_sync = training_set(rows, include_sync=False)
    assert without_sync.size == 3
    assert without_sync.excluded["sync_rows"] == 1


def test_prior_center_normalizes_and_floors_zero_weights() -> None:
    center = prior_center(WEIGHTS)
    assert center.tolist() == pytest.approx([0.35, 0.30, 0.20, 0.15])
    with pytest.raises(ValueError):
        prior_center({"skills": 0, "title": 0, "experience": 0, "location": 0})
    floored = dirichlet_concentration(prior_center({**WEIGHTS, "location": 0}), 40)
    assert floored[3] == 0.5
    assert floored[0] == pytest.approx(40 * 35 / 85)


def test_largest_remainder_keeps_a_total_of_100() -> None:
    assert sum(largest_remainder([33.4, 33.3, 33.3, 0.0])) == 100
    assert largest_remainder([35.2, 29.6, 20.1, 15.1]) == [35, 30, 20, 15]


def test_log_likelihood_matches_the_bernoulli_formula() -> None:
    eta = np.asarray([[[-2.0, 0.0, 3.0]]])
    labels = np.asarray([0, 1, 1])
    probability = 1 / (1 + np.exp(-eta))
    expected = np.log(np.where(labels == 1, probability, 1 - probability))
    assert log_likelihood(eta, labels) == pytest.approx(expected)


def test_linear_predictor_rebuilds_the_score_from_the_weights() -> None:
    center = prior_center(WEIGHTS)
    signals = np.asarray([[1.0, 1.0, 1.0, 1.0], [0.0, 0.0, 0.0, 0.0]])
    inputs = FitInputs(signals, np.asarray([1, 0]), np.zeros(2), center, 40, 0.5, 0.0)
    draws = {
        "alpha": np.zeros((1, 1)),
        "tau": np.full((1, 1), 2.0),
        "weights": center.reshape(1, 1, 4),
    }
    current = linear_predictor("current_score", draws, inputs, signals, np.zeros(2))
    learned = linear_predictor("learned_weights", draws, inputs, signals, np.zeros(2))
    assert current.reshape(-1).tolist() == pytest.approx([1.0, -1.0])
    assert learned == pytest.approx(current)


def test_weights_move_only_with_a_probable_direction_and_better_loo() -> None:
    center = prior_center(WEIGHTS)
    rng = np.random.default_rng(1)
    draws = rng.dirichlet([10, 50, 20, 20], size=(4, 500))  # title well above 0.30
    settings = BayesianRelevanceWeightsConfig()

    held = weight_summary(draws, center, 0.9, settings, improves=False)
    moved = weight_summary(draws, center, 0.9, settings, improves=True)

    assert {row["verdict"] for row in held["by_signal"].values()} == {"keep"}  # type: ignore[union-attr]
    assert held["verdict"] == "keep_current"
    by_signal = moved["by_signal"]
    assert isinstance(by_signal, dict)
    assert by_signal["title"]["verdict"] == "raise"
    assert by_signal["skills"]["verdict"] == "lower"
    assert moved["verdict"] == "change_suggested"
    assert sum(moved["suggested"].values()) == 100  # type: ignore[union-attr]
    needed = labels_needed(moved, 35, settings)
    assert needed["labels"] >= 35  # type: ignore[operator]


def test_reliability_bins_keep_ties_together_and_brier_scores() -> None:
    probabilities = [0.2, 0.2, 0.2, 0.8, 0.8, 0.9]
    labels = [0, 0, 1, 1, 1, 1]

    bins = reliability_bins(probabilities, labels, bins=4)

    assert sum(int(row["rows"]) for row in bins) == 6  # type: ignore[call-overload]
    assert [row["rows"] for row in bins][0] == 3  # the three 0.2s share one bin
    score = brier(probabilities, labels)
    assert score["brier"] == pytest.approx(
        np.mean((np.asarray(probabilities) - labels) ** 2), abs=1e-4
    )


def test_paired_elpd_difference_and_loo_probability() -> None:
    better = LooResult(np.log(np.asarray([0.9, 0.8, 0.7])), np.zeros(3))
    other = LooResult(np.log(np.asarray([0.5, 0.5, 0.5])), np.zeros(3))

    difference = elpd_difference(better, other)

    assert difference["difference"] == pytest.approx(better.elpd - other.elpd, abs=1e-3)
    assert difference["se"] > 0
    assert loo_probability(better, [1, 0, 1]).tolist() == pytest.approx([0.9, 0.2, 0.7])
    assert exact_elpd(np.log(np.asarray([0.2, 0.4]))) == pytest.approx(math.log(0.3))
    with pytest.raises(ValueError):
        elpd_difference(better, LooResult(np.zeros(2), np.zeros(2)))


def test_too_few_labels_reports_without_fitting() -> None:
    def never(*_: object) -> SampledFit:
        raise AssertionError("must not sample")

    result = fit_relevance_weights(
        _rows(10),
        weights=WEIGHTS,
        settings=BayesianRelevanceWeightsConfig(),
        thresholds=DiagnosticThresholds(),
        sampler=never,
    )

    assert result.report["status"] == "insufficient_data"
    assert "30" in str(result.report["message"])
    assert result.draws == {}


def test_models_that_fail_the_gate_report_no_posterior() -> None:
    result = fit_relevance_weights(
        _rows(40),
        weights=WEIGHTS,
        settings=_settings(sensitivity={"without_sync": False, "prior_strengths": []}),
        thresholds=DiagnosticThresholds(),
        sampler=_stuck_sampler,
        postings=[("p-1", [1.0, 1.0, 1.0, 1.0])],
    )

    report = result.report
    assert report["status"] == "not_reported"
    assert report["weights"] is None
    models = report["models"]
    assert isinstance(models, dict)
    for row in models.values():
        assert row["status"] == "failed_diagnostics"
        assert "loo" not in row and "parameters" not in row
        assert any("divergent" in failure for failure in row["diagnostics"]["failures"])
    assert all(row["status"] == "not_compared" for row in report["comparisons"])  # type: ignore[union-attr]
    assert result.draws == {} and result.postings == []


# --- with ArviZ (PSIS-LOO) ----------------------------------------------------------------


def test_full_report_with_a_stand_in_sampler() -> None:
    pytest.importorskip("arviz")
    result = fit_relevance_weights(
        _rows(40),
        weights=WEIGHTS,
        settings=BayesianRelevanceWeightsConfig(),
        thresholds=DiagnosticThresholds(),
        sampler=_iid_sampler(),
        postings=[("high", [1.0, 1.0, 1.0, 1.0]), ("mid", [0.5, 0.5, 0.5, 0.5])],
    )

    report = result.report
    assert report["status"] in {"keep_current", "change_suggested"}
    assert report["model_card"]["stage"] == "B3"  # type: ignore[index]
    assert report["model_card"]["advisory"] is True  # type: ignore[index]
    models = report["models"]
    assert isinstance(models, dict)
    assert set(models) == {"baseline", "current_score", "learned_weights", "weights_plus_fit"}
    learned = models["learned_weights"]
    assert learned["status"] == "passed"
    assert learned["loo"]["reliable"] in {True, False}
    assert learned["reliability"]
    assert "reliability" not in models["baseline"]
    assert models["weights_plus_fit"]["rows"] == 32  # rows without a fit score are left out
    pairs = {(row["model"], row["against"]) for row in report["comparisons"]}  # type: ignore[union-attr]
    assert ("learned_weights", "current_score") in pairs
    assert ("weights_plus_fit", "learned_weights") in pairs
    variants = {row["variant"] for row in report["sensitivity"]}  # type: ignore[union-attr]
    assert variants == {"prior_strength_20", "prior_strength_80", "without_sync"}
    assert report["prior_predictive"]["passed"] is True  # type: ignore[index]
    blend = report["priority_fit_weight"]
    assert isinstance(blend, dict) and 0 <= blend["low"] <= blend["high"] < 1
    assert set(result.draws) == set(models)
    by_job = {row["job_id"]: row for row in result.postings}
    assert by_job["high"]["extrapolated"] is True
    assert by_job["mid"]["extrapolated"] is False
    assert 0 <= by_job["mid"]["low"] <= by_job["mid"]["mean"] <= by_job["mid"]["high"] <= 1


def _store_labels(private_root: Path, rows: list[dict[str, object]]) -> None:
    store = TrackingLabelStore(tracking_labels_path(private_root))
    for index, row in enumerate(rows):
        positive = row["label"] == 1
        store.append(
            TrackingLabel(
                source=TrackingSource.TRACKING,
                job_id=str(row["job_id"]),
                status="saved" if positive else "archived",
                label_class=TrackingClass.POSITIVE if positive else TrackingClass.NEGATIVE,
                features={
                    key: value  # type: ignore[misc]
                    for key, value in row.items()
                    if key.endswith("_signal") or key == "fit_score"
                },
                captured=True,
                labeled_at=START + timedelta(minutes=index),
            )
        )


def test_run_writes_private_outputs_tied_to_a_snapshot(tmp_path: Path) -> None:
    pytest.importorskip("arviz")
    private_root = tmp_path / "private"
    _store_labels(private_root, _rows(36))
    mart = {
        "opportunities": [
            {"job_id": "now-1", "score_components": {name: {"signal": 0.6} for name in SIGNALS}},
            {"job_id": "no-signals"},
        ]
    }

    result, paths = run_relevance_model(
        Config(), private_root, mart=mart, sampler=_iid_sampler(), now=START
    )

    folder = relevance_model_dir(private_root)
    assert Path(str(paths["report"])) == folder / "latest.json"
    report = read_relevance_report(private_root)
    assert report is not None and report["snapshot"]["verified"] is True
    assert report["snapshot"]["log_lines"] == 36
    assert report["run_id"] == "20261101-120000"
    assert (folder / "runs" / "20261101-120000.json").exists()
    with np.load(folder / "posterior.npz") as archive:
        assert "learned_weights/weights" in archive.files
    postings = json.loads((folder / "postings.json").read_text(encoding="utf-8"))
    assert set(postings["postings"]) == {"now-1"}
    assert mart_postings(mart) == [("now-1", [0.6, 0.6, 0.6, 0.6])]
    with pytest.raises(LookupError):
        run_relevance_model(Config(), private_root, snapshot_id="missing", sampler=_iid_sampler())
    assert result.report["status"] == report["status"]


@contextmanager
def _running(server: DashboardHTTPServer) -> Iterator[str]:
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _get(url: str) -> dict[str, object]:
    with urlopen(url) as response:  # noqa: S310
        return dict(json.loads(response.read()))


def test_dashboard_serves_the_latest_fit_and_says_when_it_is_stale(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    server = create_server(
        port=0, private_root=private_root, tracking_path=tmp_path / "tracking.json"
    )
    with _running(server) as base:
        empty = _get(f"{base}/api/models/b3")
        assert empty["report"] is None
        assert "fit_relevance_weights.py" in str(empty["message"])

        folder = relevance_model_dir(private_root)
        folder.mkdir(parents=True)
        report = {
            "status": "keep_current",
            "snapshot": {"log_lines": 2},
            "current_weights": {
                "skills": 35.0,
                "title": 30.0,
                "experience": 20.0,
                "location": 15.0,
            },
        }
        (folder / "latest.json").write_text(json.dumps(report), encoding="utf-8")
        _store_labels(private_root, _rows(3))
        served = _get(f"{base}/api/models/b3")

    assert served["report"] == report
    assert served["new_log_lines"] == 1
    assert served["weights_changed"] is False


# --- with PyMC (the real sampler) ----------------------------------------------------------


def test_pymc_recovers_known_weights() -> None:
    pytest.importorskip("pymc")
    pytest.importorskip("arviz")
    truth = (0.15, 0.55, 0.15, 0.15)
    settings = _settings(
        models=["current_score", "learned_weights"],
        prior_strength=8.0,
        sensitivity={"without_sync": False, "prior_strengths": []},
    )

    result = fit_relevance_weights(
        _rows(400, true_weights=truth, seed=11),
        weights=WEIGHTS,
        settings=settings,
        thresholds=DiagnosticThresholds(),
    )

    report = result.report
    models = report["models"]
    assert isinstance(models, dict)
    assert models["learned_weights"]["status"] == "passed", models["learned_weights"]
    weights = report["weights"]
    assert isinstance(weights, dict)
    by_signal = weights["by_signal"]
    for name, value in zip(SIGNALS, truth, strict=True):
        row = by_signal[name]
        assert row["low"] - 5 <= value * 100 <= row["high"] + 5, (name, row)
    # The title weight is far above the configured 30, and 400 rows show it.
    assert by_signal["title"]["probability_above_current"] > 0.9
