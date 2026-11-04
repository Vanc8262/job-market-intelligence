"""Run Phase 8 B3 on the tracking-label log and keep its results private.

``run_relevance_model`` freezes the training log as a snapshot (reusing the existing one
when nothing changed), fits ``bayesian.relevance`` on that snapshot's rows, and writes:

* ``data/private/models/b3/latest.json`` - the advisory report and model card;
* ``data/private/models/b3/posterior.npz`` - posterior draws of every model that passed;
* ``data/private/models/b3/postings.json`` - P(keep) with its interval per current posting;
* ``data/private/models/b3/runs/<run id>.json`` - a copy of each report.

The results encode your preferences, so they never go to Gold or the data repository. The
dashboard only reads ``latest.json``; it never samples.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from analytics.tracking_labels import (
    TrackingLabelStore,
    create_tracking_snapshot,
    list_tracking_snapshots,
    tracking_labels_path,
    tracking_snapshot_labels,
    training_rows,
)
from bayesian.diagnostics import DiagnosticThresholds
from bayesian.relevance import SIGNALS, RelevanceFit, Sampler, fit_relevance_weights
from comparison.label_changes import write_json_atomic
from utils.config import Config

MODEL_DIR = Path("models") / "b3"
LATEST_FILE = "latest.json"
POSTERIOR_FILE = "posterior.npz"
POSTINGS_FILE = "postings.json"


def relevance_model_dir(private_root: Path) -> Path:
    """Return where B3 results are kept."""
    return private_root / MODEL_DIR


def read_relevance_report(private_root: Path) -> dict[str, Any] | None:
    """Return the latest B3 report, or None when B3 has not been run."""
    path = relevance_model_dir(private_root) / LATEST_FILE
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("B3 report must be a JSON object")
    return payload


def mart_postings(mart: Mapping[str, object] | None) -> list[tuple[str, list[float]]]:
    """Return (job id, signals) for every current posting with all four signals."""
    rows = mart.get("opportunities") if mart else None
    postings: list[tuple[str, list[float]]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not row.get("job_id"):
            continue
        components = row.get("score_components")
        components = components if isinstance(components, dict) else {}
        values: list[float] = []
        for name in SIGNALS:
            component = components.get(name)
            signal = component.get("signal") if isinstance(component, dict) else None
            if isinstance(signal, bool) or not isinstance(signal, int | float):
                break
            values.append(float(signal))
        else:
            postings.append((str(row["job_id"]), values))
    return postings


def run_relevance_model(
    config: Config,
    private_root: Path,
    *,
    snapshot_id: str | None = None,
    mart: Mapping[str, object] | None = None,
    sampler: Sampler | None = None,
    now: datetime | None = None,
) -> tuple[RelevanceFit, dict[str, object]]:
    """Fit B3 on a verified snapshot, write the private outputs, and return the result."""
    store = TrackingLabelStore(tracking_labels_path(private_root))
    if snapshot_id is None:
        manifest, _ = create_tracking_snapshot(store, config.labels, now=now)
    else:
        found = next(
            (item for item in list_tracking_snapshots(store) if item["snapshot_id"] == snapshot_id),
            None,
        )
        if found is None:
            raise LookupError(f"No training-label snapshot {snapshot_id}")
        manifest = found
    labels = tracking_snapshot_labels(store, manifest)
    rows = training_rows(labels, config.labels)
    settings = config.bayesian.relevance_weights
    gate = config.bayesian.diagnostics
    result = fit_relevance_weights(
        rows,
        weights=config.resume_profile.weights.model_dump(),
        settings=settings,
        thresholds=DiagnosticThresholds(
            max_rhat=gate.max_rhat,
            min_ess_bulk=gate.min_ess_bulk,
            min_ess_tail=gate.min_ess_tail,
            max_divergences=gate.max_divergences,
        ),
        credible_level=config.bayesian.credible_level,
        minimum_labels=config.labels.tracking.b3_minimum,
        minimum_each=config.labels.tracking.simulator_minimum_each,
        sampler=sampler,
        postings=mart_postings(mart),
        configured_fit_weight=config.ranking.priority.fit_weight,
    )
    created = now or datetime.now(UTC)
    run_id = f"{created:%Y%m%d-%H%M%S}"
    result.report["run_id"] = run_id
    result.report["snapshot"] = {
        "snapshot_id": manifest.get("snapshot_id"),
        "log_lines": manifest.get("log_lines"),
        "log_sha256": manifest.get("log_sha256"),
        "verified": True,
    }
    paths = write_relevance_fit(private_root, result, run_id)
    return result, paths


def write_relevance_fit(private_root: Path, result: RelevanceFit, run_id: str) -> dict[str, object]:
    """Write the report, draws, and posting probabilities; return where they went."""
    folder = relevance_model_dir(private_root)
    (folder / "runs").mkdir(parents=True, exist_ok=True)
    write_json_atomic(folder / LATEST_FILE, result.report)
    write_json_atomic(folder / "runs" / f"{run_id}.json", result.report)
    written: dict[str, object] = {"report": str(folder / LATEST_FILE)}
    if result.draws:
        arrays: dict[str, Any] = {
            f"{model}/{name}": values
            for model, draws in result.draws.items()
            for name, values in draws.items()
        }
        buffer = io.BytesIO()
        np.savez_compressed(buffer, **arrays)
        (folder / POSTERIOR_FILE).write_bytes(buffer.getvalue())
        written["posterior"] = str(folder / POSTERIOR_FILE)
    write_json_atomic(
        folder / POSTINGS_FILE,
        {"run_id": run_id, "postings": _by_job(result.postings)},
    )
    written["postings"] = str(folder / POSTINGS_FILE)
    return written


def _by_job(postings: Sequence[Mapping[str, object]]) -> dict[str, dict[str, object]]:
    return {
        str(row["job_id"]): {key: value for key, value in row.items() if key != "job_id"}
        for row in postings
    }


__all__ = [
    "mart_postings",
    "read_relevance_report",
    "relevance_model_dir",
    "run_relevance_model",
    "write_relevance_fit",
]
