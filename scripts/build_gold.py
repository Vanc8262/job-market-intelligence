"""Command-line entry point for Silver-to-Gold market summaries."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analytics import SilverToGoldRunner
from profiles.profile_source import resolve_resume_profile
from utils.config import Config
from utils.logger import configure_logging


def build_parser() -> argparse.ArgumentParser:
    """Build command-line arguments for Gold summary generation."""
    parser = argparse.ArgumentParser(description="Build Gold summaries from Silver jobs")
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="Include expired, removed, duplicate, and inactive postings",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Build all Gold datasets and print a machine-readable summary."""
    args = build_parser().parse_args(argv)
    config = Config()
    configure_logging(config)
    profile, source = resolve_resume_profile(config)
    result = SilverToGoldRunner(config, resume_profile=profile, profile_source=source).run(
        include_inactive=args.include_inactive
    )
    print(json.dumps(result.summary(), indent=2, sort_keys=True))
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
