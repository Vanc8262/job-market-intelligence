"""Apply the latest candidate collection evidence to posting lifecycle state."""

# ruff: noqa: E402

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline import PostingLifecycleRunner
from utils.config import Config
from utils.logger import configure_logging


def main() -> int:
    """Update lifecycle from the latest persisted collection summary."""
    configure_logging(Config())
    result = PostingLifecycleRunner().run()
    print(json.dumps(result.summary(), indent=2, sort_keys=True))
    return 1 if result.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
