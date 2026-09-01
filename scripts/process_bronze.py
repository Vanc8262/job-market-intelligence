"""Command-line entry point for Bronze-to-Silver processing."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline import BronzeToSilverRunner
from utils.config import Config
from utils.logger import configure_logging


def build_parser() -> argparse.ArgumentParser:
    """Build command-line arguments for Bronze-to-Silver processing."""
    parser = argparse.ArgumentParser(description="Normalize Bronze jobs into Silver")
    parser.add_argument(
        "--target",
        action="append",
        dest="targets",
        help="Configured target name; repeat to process multiple targets",
    )
    parser.add_argument("--limit", type=int, help="Maximum Bronze snapshots for this run")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Process Bronze snapshots and print a machine-readable summary."""
    args = build_parser().parse_args(argv)
    config = Config()
    configure_logging(config)
    result = BronzeToSilverRunner().run(target_names=args.targets, limit=args.limit)
    print(json.dumps(result.summary(), indent=2, sort_keys=True))
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
