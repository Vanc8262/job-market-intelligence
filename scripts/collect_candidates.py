"""Run configured candidate searches through Bronze, Silver, Gold, and quality."""

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
from pipeline import (
    BronzeToSilverRunner,
    CandidateCollectionRunner,
    DataQualityGateway,
    PostingLifecycleRunner,
)
from profiles.profile_source import resolve_resume_profile
from utils.config import Config
from utils.logger import configure_logging


def build_parser() -> argparse.ArgumentParser:
    """Build candidate collection command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Collect configured candidate searches and evaluate quality"
    )
    parser.add_argument(
        "--query-name",
        action="append",
        dest="query_names",
        help="Configured query name; repeat to select multiple searches",
    )
    parser.add_argument(
        "--target",
        action="append",
        dest="targets",
        help="Enabled target name; repeat to select multiple targets",
    )
    parser.add_argument(
        "--limit-per-target",
        type=int,
        help="Override the profile's unique-reference cap per target",
    )
    parser.add_argument(
        "--skip-downstream",
        action="store_true",
        help="Stop after Bronze collection without rebuilding Silver and Gold",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run collection and, by default, all downstream quality stages."""
    args = build_parser().parse_args(argv)
    config = Config()
    configure_logging(config)
    collection = CandidateCollectionRunner().run(
        query_names=args.query_names,
        target_names=args.targets,
        per_target_limit=args.limit_per_target,
    )
    output: dict[str, object] = {"collection": collection.summary()}
    if args.skip_downstream:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 1 if collection.extraction_failure_rate > 0 else 0

    selected_targets = [outcome.target_name for outcome in collection.outcomes]
    processing = BronzeToSilverRunner().run(target_names=selected_targets)
    lifecycle = PostingLifecycleRunner().run(collection_summary=collection.summary())
    profile, source = resolve_resume_profile(config)
    gold = SilverToGoldRunner(config, resume_profile=profile, profile_source=source).run()
    quality = DataQualityGateway().run(collection_summary=collection.summary())
    output.update(
        {
            "processing": processing.summary(),
            "lifecycle": lifecycle.summary(),
            "gold": gold.summary(),
            "quality": quality.summary(),
        }
    )
    print(json.dumps(output, indent=2, sort_keys=True))
    return 1 if processing.failed or lifecycle.failures or gold.failed or not quality.passed else 0


if __name__ == "__main__":
    raise SystemExit(main())
