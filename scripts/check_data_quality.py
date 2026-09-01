"""Command-line entry point for the current data-quality gateway."""

# ruff: noqa: E402

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline import DataQualityGateway
from utils.config import Config
from utils.logger import configure_logging


def main() -> int:
    """Evaluate Silver and the latest collection summary."""
    config = Config()
    configure_logging(config)
    result = DataQualityGateway(config).run()
    print(json.dumps(result.summary(), indent=2, sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
