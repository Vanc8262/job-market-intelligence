"""Command-line entry point for configured extraction into Bronze storage."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline import ExtractionRunner
from utils.config import Config
from utils.logger import configure_logging


def build_parser() -> argparse.ArgumentParser:
    """Build command-line arguments for configured extraction."""
    parser = argparse.ArgumentParser(description="Extract configured job targets into Bronze")
    parser.add_argument("--query", default="", help="Text query; empty matches all postings")
    parser.add_argument("--location", help="Optional location filter")
    parser.add_argument(
        "--target",
        action="append",
        dest="targets",
        help="Enabled target name; repeat to run multiple targets",
    )
    parser.add_argument("--limit", type=int, help="Maximum postings per target")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run configured extraction and print a machine-readable summary."""
    args = build_parser().parse_args(argv)
    config = Config()
    configure_logging(config)
    batch = ExtractionRunner().run(
        args.query,
        args.location,
        target_names=args.targets,
        limit=args.limit,
    )
    print(json.dumps(batch.summary(), indent=2, sort_keys=True))
    return 1 if batch.targets_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
