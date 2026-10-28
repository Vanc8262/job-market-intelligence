"""Freeze, list, and verify snapshots of the private requirement-label log.

  python scripts/label_snapshots.py create   # freeze the current labels
  python scripts/label_snapshots.py list     # newest first, each checked against the log
  python scripts/label_snapshots.py verify   # exit 1 if any snapshot no longer matches

A snapshot records the first N lines of the append-only log and their SHA-256, so a
Phase 7 number can name exactly which labels it used. Output is counts and hashes only,
never requirement or resume text. The dashboard's Labels > Changes view does the same.
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

from comparison.label_changes import create_snapshot, list_snapshots
from comparison.labels import RequirementLabelStore, labels_path
from comparison.runner import load_results
from utils.config import Config


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["create", "list", "verify"])
    parser.add_argument(
        "--private-root", type=Path, default=None, help="Defaults to settings.paths.private_dir"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one snapshot command and print JSON."""
    args = build_parser().parse_args(argv)
    config = Config()
    private_root = Path(args.private_root or config.settings.paths.private_dir)
    store = RequirementLabelStore(labels_path(private_root))
    if args.command == "create":
        results = load_results(private_root) or {}
        model = results.get("model")
        manifest, created = create_snapshot(
            store, config.labels, model if isinstance(model, dict) else None
        )
        print(json.dumps({"created": created, "snapshot": manifest}, indent=2, sort_keys=True))
        return 0
    snapshots = list_snapshots(store)
    if args.command == "list":
        print(json.dumps(snapshots, indent=2, sort_keys=True))
        return 0
    broken = [item["snapshot_id"] for item in snapshots if not item["verified"]]
    print(json.dumps({"snapshots": len(snapshots), "unverified": broken}, indent=2))
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
