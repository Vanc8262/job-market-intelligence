"""Log a collection run and report whether daily, weekly, or biweekly runs are enough.

Usage (the scheduled GitHub Actions workflow runs the first form after each collection):

  python scripts/cadence_report.py --record --scheduled --run-seconds 412 \
      --summary-file "$GITHUB_STEP_SUMMARY"
  python scripts/cadence_report.py            # report only, from the existing log

Writes gold/cadence/observations.jsonl (one line per run), gold/cadence/latest.json,
and gold/cadence/latest.md. Output contains posting IDs and counts, never resume data.
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

from analytics.cadence import (
    OBSERVATIONS_FILE,
    append_observation,
    cadence_report,
    load_observations,
    observation_from_state,
    report_markdown,
)
from models.lifecycle import LifecycleState
from utils.config import Config


def build_parser() -> argparse.ArgumentParser:
    """Build command-line arguments for cadence monitoring."""
    parser = argparse.ArgumentParser(description="Log collection runs and compare cadences")
    parser.add_argument("--record", action="store_true", help="Log the latest collection run")
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Mark the logged run as a scheduled run (only these feed the comparison)",
    )
    parser.add_argument("--run-seconds", type=float, help="How long the collection took")
    parser.add_argument("--summary-file", type=Path, help="Append the Markdown report here")
    parser.add_argument("--minimum-days", type=int, default=28)
    parser.add_argument("--max-missed-share", type=float, default=0.05)
    return parser


def _folder_bytes(folder: Path) -> int:
    return sum(path.stat().st_size for path in folder.rglob("*") if path.is_file())


def main(argv: list[str] | None = None) -> int:
    """Optionally log the latest run, then write and print the cadence report."""
    args = build_parser().parse_args(argv)
    paths = Config().settings.paths
    gold = Path(paths.gold_dir)
    log_path = gold / OBSERVATIONS_FILE
    if args.record:
        state_path = gold / "lifecycle" / "state.json"
        if not state_path.exists():
            print(json.dumps({"error": f"no lifecycle state at {state_path}"}))
            return 1
        state = LifecycleState.model_validate_json(state_path.read_text(encoding="utf-8"))
        data_root = Path(paths.bronze_dir).parent
        observation = observation_from_state(
            state,
            scheduled=args.scheduled,
            run_seconds=args.run_seconds,
            data_bytes=_folder_bytes(data_root),
        )
        append_observation(log_path, observation)
    report = cadence_report(
        load_observations(log_path),
        minimum_days=args.minimum_days,
        max_missed_share=args.max_missed_share,
    )
    markdown = report_markdown(report)
    (gold / "cadence").mkdir(parents=True, exist_ok=True)
    (gold / "cadence" / "latest.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (gold / "cadence" / "latest.md").write_text(markdown, encoding="utf-8")
    if args.summary_file:
        with args.summary_file.open("a", encoding="utf-8") as stream:
            stream.write(markdown)
    print(json.dumps({key: report.get(key) for key in ("status", "recommendation", "reason")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
