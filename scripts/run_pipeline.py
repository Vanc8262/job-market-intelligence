"""Phase 1 pipeline entry point for validating foundation infrastructure."""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.config import Config
from utils.logger import configure_logging, logger


def main() -> None:
    """Validate configuration and initialize logging for the future pipeline."""
    config = Config()
    configure_logging(config)
    logger.info("JMIP environment: {environment}", environment=config.settings.environment)
    logger.info("Enabled sources: {sources}", sources=", ".join(config.sources.enabled_sources))
    logger.success("Phase 1 foundation initialized successfully.")


if __name__ == "__main__":
    main()
