"""Pull the latest collected data and re-rank it against your approved resume.

Collection runs in GitHub Actions and commits Bronze, Silver, and Gold to the private
data repository. On your computer, set ``JMIP_PIPELINE_DATA_DIR`` (in ``.env``) to your
clone of that repository and run:

  python scripts/sync_data.py

It discards local edits in the data clone (Gold files rebuilt here are derived outputs),
fast-forwards it to the collector's latest commit, then rebuilds Gold so relevance uses
your approved resume and ``config/resume_profile.local.yaml``, and compares every
current opportunity with that resume (Phase 6.3; results stay in ``data/private``).
Application tracking and resumes are stored elsewhere and are never touched.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analytics import SilverToGoldRunner
from comparison.runner import ApplicationComparisonRunner
from profiles.profile_source import resolve_resume_profile
from utils.config import Config
from utils.logger import configure_logging


def _git(data_root: Path, *args: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is not installed or not on PATH")
    result = subprocess.run(  # noqa: S603
        [git, "-C", str(data_root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return result.stdout.strip()


def build_parser() -> argparse.ArgumentParser:
    """Build command-line arguments for syncing collected data."""
    parser = argparse.ArgumentParser(description="Pull collected data and rebuild Gold")
    parser.add_argument("--branch", default="main", help="Data repository branch")
    parser.add_argument("--skip-pull", action="store_true", help="Only rebuild Gold")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Update the data clone, then rebuild Gold with the personal profile."""
    args = build_parser().parse_args(argv)
    config = Config()
    configure_logging(config)
    if not os.getenv("JMIP_PIPELINE_DATA_DIR", "").strip():
        print(
            json.dumps(
                {
                    "error": "Set JMIP_PIPELINE_DATA_DIR in .env to your jmip-data clone "
                    "(for example JMIP_PIPELINE_DATA_DIR=../jmip-data)."
                }
            )
        )
        return 1
    data_root = Path(config.settings.paths.gold_dir).parent
    output: dict[str, object] = {"data_dir": str(data_root)}
    try:
        if not args.skip_pull:
            if not (data_root / ".git").exists():
                raise RuntimeError(f"{data_root} is not a git clone of the data repository")
            _git(data_root, "fetch", "--quiet", "origin", args.branch)
            _git(data_root, "reset", "--quiet", "--hard", f"origin/{args.branch}")
            _git(data_root, "clean", "--quiet", "-fd")
            output["data_commit"] = _git(data_root, "log", "-1", "--format=%h %s")
    except (RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        print(json.dumps({**output, "error": str(detail).strip()}, indent=2))
        return 1
    profile, source = resolve_resume_profile(config)
    gold = SilverToGoldRunner(config, resume_profile=profile, profile_source=source).run()
    comparison = ApplicationComparisonRunner(config).run()
    output.update(
        {"profile_source": source, "gold": gold.summary(), "comparison": comparison.summary()}
    )
    print(json.dumps(output, indent=2, sort_keys=True, default=str))
    return 1 if gold.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
