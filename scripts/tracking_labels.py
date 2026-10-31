"""Manage the B3 training labels logged from application-tracking decisions.

  python scripts/tracking_labels.py summary               # counts and B3 progress
  python scripts/tracking_labels.py sync                  # log decisions the log lacks
  python scripts/tracking_labels.py snapshot              # freeze the log
  python scripts/tracking_labels.py snapshots             # list snapshots, each verified
  python scripts/tracking_labels.py export [--snapshot ID]  # B3 training rows as JSON

The dashboard logs every status change you save; ``sync`` adds decisions made before the
log existed, with today's signals. Output is counts, scores, and posting ids, never
resume text. The dashboard's Labels > Training data view does the same.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analytics.tracking_labels import (
    TrackingLabelStore,
    create_tracking_snapshot,
    decision_context,
    list_tracking_snapshots,
    review_tracking,
    summarize_tracking,
    sync,
    tracking_labels_path,
    tracking_snapshot_labels,
    training_rows,
    unlogged,
)
from comparison.runner import load_results
from dashboard.app import DEFAULT_TRACKING_PATH, configured_gold_root, load_opportunities_mart
from models.application_tracking import ApplicationTrackingState
from utils.config import Config


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["summary", "sync", "snapshot", "snapshots", "export"])
    parser.add_argument("--snapshot", default=None, help="export: the snapshot id to export")
    parser.add_argument(
        "--private-root", type=Path, default=None, help="Defaults to settings.paths.private_dir"
    )
    parser.add_argument("--tracking", type=Path, default=DEFAULT_TRACKING_PATH)
    parser.add_argument("--mart", type=Path, default=None, help="Defaults to the Gold mart")
    return parser


def tracking_statuses(path: Path) -> dict[str, str]:
    """Return each posting's tracking status from the dashboard's state file (read only)."""
    if not path.exists():
        return {}
    state = ApplicationTrackingState.model_validate_json(path.read_text(encoding="utf-8"))
    return {job_id: record.status.value for job_id, record in state.records.items()}


def main(argv: list[str] | None = None) -> int:
    """Run one command and print JSON; exit 1 for an unknown or broken snapshot."""
    args = build_parser().parse_args(argv)
    config = Config()
    private_root = Path(args.private_root or config.settings.paths.private_dir)
    store = TrackingLabelStore(tracking_labels_path(private_root))
    if args.command == "snapshot":
        manifest, created = create_tracking_snapshot(store, config.labels)
        print(json.dumps({"created": created, "snapshot": manifest}, indent=2, sort_keys=True))
        return 0
    if args.command == "snapshots":
        print(json.dumps(list_tracking_snapshots(store), indent=2, sort_keys=True))
        return 0
    if args.command == "export":
        labels = store.all()
        if args.snapshot:
            chosen = next(
                (
                    item
                    for item in list_tracking_snapshots(store)
                    if item["snapshot_id"] == args.snapshot
                ),
                None,
            )
            if chosen is None:
                print(json.dumps({"error": f"No snapshot {args.snapshot}"}))
                return 1
            try:
                labels = tracking_snapshot_labels(store, chosen)
            except ValueError as exc:
                print(json.dumps({"error": str(exc)}))
                return 1
        rows = training_rows(labels, config.labels)
        print(json.dumps({"snapshot_id": args.snapshot, "rows": rows}, indent=2, sort_keys=True))
        return 0
    statuses = tracking_statuses(args.tracking)
    mart_path = args.mart or configured_gold_root() / "current_opportunities" / "mart.json"
    try:
        mart = load_opportunities_mart(mart_path)
    except (FileNotFoundError, ValueError):
        mart = None
    if args.command == "sync":
        context = decision_context(mart, load_results(private_root))
        added = sync(store, statuses, context, config.labels)
        print(json.dumps({"added": len(added), "job_ids": [label.job_id for label in added]}))
        return 0
    labels = store.all()
    postings = decision_context(mart, None).postings
    summary = summarize_tracking(
        review_tracking(labels, statuses, postings, config.labels),
        unlogged(labels, statuses),
        config.labels,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
