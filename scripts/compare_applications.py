"""Compare current opportunities with the approved resume (Phase 6.3).

  python scripts/compare_applications.py

Writes data/private/comparisons/latest.json (private: it quotes resume evidence) and
prints a summary without resume text. scripts/sync_data.py runs this automatically.
"""

# ruff: noqa: E402

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from comparison.runner import ApplicationComparisonRunner
from utils.config import Config
from utils.logger import configure_logging


def main(argv: list[str] | None = None) -> int:
    """Run the comparison and print its summary."""
    del argv
    config = Config()
    configure_logging(config)
    result = ApplicationComparisonRunner(config).run()
    print(json.dumps(result.summary(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
