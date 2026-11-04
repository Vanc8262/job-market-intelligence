"""Fit Phase 8 B3: relevance weights learned from your tracking decisions.

  python scripts/fit_relevance_weights.py                 # snapshot the log, fit, report
  python scripts/fit_relevance_weights.py --snapshot ID   # refit a saved snapshot exactly

Needs the optional ``bayes`` extra (``pip install -e ".[bayes]"``). Results are advisory
and stay in ``data/private/models/b3/``; changing ``weights:`` in your resume profile is
still your edit. Output is counts, weights, and model statistics, never posting or resume
text. Exits 1 when there are too few labels, a snapshot is missing or broken, or the
learned-weights model fails the diagnostics gate.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analytics.relevance_model import run_relevance_model
from dashboard.app import configured_gold_root, load_opportunities_mart
from utils.config import Config


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", default=None, help="Fit this saved snapshot id")
    parser.add_argument(
        "--private-root", type=Path, default=None, help="Defaults to settings.paths.private_dir"
    )
    parser.add_argument("--mart", type=Path, default=None, help="Defaults to the Gold mart")
    return parser


def summary(report: dict[str, object], paths: dict[str, object]) -> dict[str, object]:
    """Return the short JSON printed on stdout."""
    weights = report.get("weights")
    models = report.get("models")
    return {
        "status": report.get("status"),
        "message": report.get("message"),
        "snapshot": report.get("snapshot"),
        "data": report.get("data"),
        "models": {
            name: {
                "status": row.get("status"),
                "loo": row.get("loo"),
                "brier": row.get("brier"),
            }
            for name, row in (models.items() if isinstance(models, dict) else [])
        },
        "comparisons": report.get("comparisons"),
        "current_weights": report.get("current_weights"),
        "suggested_weights": weights.get("suggested") if isinstance(weights, dict) else None,
        "verdicts": (
            {name: row.get("verdict") for name, row in weights["by_signal"].items()}
            if isinstance(weights, dict)
            else None
        ),
        "priority_fit_weight": report.get("priority_fit_weight"),
        "outputs": paths,
    }


def main(argv: list[str] | None = None) -> int:
    """Fit B3 and print a JSON summary."""
    args = build_parser().parse_args(argv)
    missing = [name for name in ("pymc", "arviz") if importlib.util.find_spec(name) is None]
    if missing:
        print(
            json.dumps(
                {
                    "error": f"B3 needs the optional bayes extra (missing: {', '.join(missing)})",
                    "fix": 'pip install -e ".[bayes]"',
                }
            )
        )
        return 1
    config = Config()
    if not config.bayesian.relevance_weights.enabled:
        print(json.dumps({"error": "relevance_weights is disabled in config/bayesian.yaml"}))
        return 1
    private_root = Path(args.private_root or config.settings.paths.private_dir)
    mart_path = args.mart or configured_gold_root() / "current_opportunities" / "mart.json"
    try:
        mart = load_opportunities_mart(mart_path)
    except (FileNotFoundError, ValueError):
        mart = None
    try:
        result, paths = run_relevance_model(
            config, private_root, snapshot_id=args.snapshot, mart=mart
        )
    except (LookupError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(summary(result.report, paths), indent=2, sort_keys=True))
    return 0 if result.report.get("status") in {"keep_current", "change_suggested"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
