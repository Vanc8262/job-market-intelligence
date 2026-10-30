"""Print the Labels Scorecard: agreement between 6.3 verdicts and your labels.

  python scripts/label_scorecard.py                      # current labels, with replay
  python scripts/label_scorecard.py --snapshot ID        # the labels a snapshot froze
  python scripts/label_scorecard.py --no-replay          # skip re-judging labeled lines

Agreement is reported as Beta-Binomial posteriors with credible intervals; only randomly
sampled review-queue labels feed the accuracy estimate. Output is counts, rates, and the
requirement text of lines the replay fixed or regressed, never resume text. The dashboard's
Labels > Scorecard view shows the same numbers.
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

from comparison.label_changes import current_snapshot, list_snapshots, snapshot_labels
from comparison.labels import RequirementLabelStore, labels_path
from comparison.runner import load_results
from comparison.scorecard import build_scorecard, resume_judge
from utils.config import Config


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", default=None, help="Snapshot id to score instead")
    parser.add_argument("--no-replay", action="store_true", help="Skip the replay")
    parser.add_argument(
        "--private-root", type=Path, default=None, help="Defaults to settings.paths.private_dir"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Compute the scorecard and print it as JSON; exit 1 for an unknown or broken snapshot."""
    args = build_parser().parse_args(argv)
    config = Config()
    private_root = Path(args.private_root or config.settings.paths.private_dir)
    store = RequirementLabelStore(labels_path(private_root))
    if args.snapshot:
        manifest = next(
            (item for item in list_snapshots(store) if item["snapshot_id"] == args.snapshot), None
        )
        if manifest is None:
            print(json.dumps({"error": f"No snapshot {args.snapshot}"}))
            return 1
        try:
            labels = snapshot_labels(store, manifest)
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}))
            return 1
        snapshot_id: object = args.snapshot
    else:
        labels = store.all()
        snapshot = current_snapshot(store)
        snapshot_id = snapshot["snapshot_id"] if snapshot else None
    judge, reason = (None, "Skipped") if args.no_replay else resume_judge(config, private_root)
    card = build_scorecard(
        labels, load_results(private_root), config.labels, judge=judge, replay_unavailable=reason
    )
    card["snapshot_id"] = snapshot_id
    card["current_model_version"] = config.comparison.version
    print(json.dumps(card, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
