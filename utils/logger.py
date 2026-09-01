"""Centralized Loguru logger configuration for JMIP."""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Protocol, cast, runtime_checkable

from loguru import logger

from utils.config import Config

LOG_CONTEXT_DEFAULTS = {
    "component": "jmip",
    "source": "-",
    "job_id": "-",
    "ingestion_batch_id": "-",
    "run_id": "-",
    "event": "-",
}

LogLevel = Literal["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL", "SCRAPER"]


@runtime_checkable
class BoundLogger(Protocol):
    """Small protocol for Loguru logger objects returned by ``bind``."""

    def bind(self, **kwargs: object) -> BoundLogger:
        """Return a logger with additional context."""

    def log(self, level: str, message: str, *args: object, **kwargs: object) -> None:
        """Log a message at a dynamic level."""

    def info(self, message: str, *args: object, **kwargs: object) -> None:
        """Log an informational message."""

    def success(self, message: str, *args: object, **kwargs: object) -> None:
        """Log a success message."""

    def warning(self, message: str, *args: object, **kwargs: object) -> None:
        """Log a warning message."""

    def error(self, message: str, *args: object, **kwargs: object) -> None:
        """Log an error message."""


@dataclass(frozen=True)
class LogContext:
    """Structured context attached to JMIP log events."""

    component: str = "jmip"
    source: str = "-"
    job_id: str = "-"
    ingestion_batch_id: str = "-"
    run_id: str = "-"

    def clean(self) -> dict[str, str]:
        """Return context values with logging defaults applied."""
        defaults = LOG_CONTEXT_DEFAULTS.copy()
        defaults.update({key: value for key, value in asdict(self).items() if value})
        return defaults


def configure_logging(config: Config | None = None) -> None:
    """Configure console, application, and error logging sinks."""
    active_config = config or Config()
    settings = active_config.settings
    logging_config = active_config.logging.model_dump()
    file_config = logging_config.get("files", {})
    console_config = logging_config.get("console", {})
    format_config = logging_config.get("formats", {})

    logger.remove()
    logger.configure(extra=LOG_CONTEXT_DEFAULTS)
    _ensure_scraper_level()

    if console_config.get("enabled", True):
        logger.add(
            sys.stderr,
            level=console_config.get("level", settings.logging.level),
            colorize=console_config.get("colorize", True),
            format=format_config.get("console"),
        )

    if file_config.get("enabled", True):
        log_dir = Path(settings.paths.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            file_config.get("application_log", str(log_dir / "jmip.log")),
            level=settings.logging.level,
            rotation=file_config.get("rotation", "00:00"),
            retention=file_config.get("retention", "30 days"),
            compression=file_config.get("compression", "zip"),
            format=format_config.get("file"),
        )
        logger.add(
            file_config.get("error_log", str(log_dir / "errors.log")),
            level="ERROR",
            rotation=file_config.get("rotation", "00:00"),
            retention=file_config.get("retention", "30 days"),
            compression=file_config.get("compression", "zip"),
            format=format_config.get("file"),
        )


def bind_context(
    *,
    component: str = "jmip",
    source: str | None = None,
    job_id: str | None = None,
    ingestion_batch_id: str | None = None,
    run_id: str | None = None,
) -> BoundLogger:
    """Return a Loguru logger bound with standard JMIP context fields."""
    context = LogContext(
        component=component,
        source=source or "-",
        job_id=job_id or "-",
        ingestion_batch_id=ingestion_batch_id or "-",
        run_id=run_id or "-",
    )
    return cast(BoundLogger, logger.bind(**context.clean()))


def log_event(
    level: LogLevel,
    message: str,
    *,
    component: str = "jmip",
    event: str | None = None,
    source: str | None = None,
    job_id: str | None = None,
    ingestion_batch_id: str | None = None,
    run_id: str | None = None,
    **fields: object,
) -> None:
    """Write a structured JMIP log event with standard context fields."""
    bound_logger = bind_context(
        component=component,
        source=source,
        job_id=job_id,
        ingestion_batch_id=ingestion_batch_id,
        run_id=run_id,
    )
    message_fields: dict[str, object] = {
        "component": component,
        "source": source or "-",
        "job_id": job_id or "-",
        "ingestion_batch_id": ingestion_batch_id or "-",
        "run_id": run_id or "-",
        "event": event or "-",
        **fields,
    }
    bound_logger.bind(event=event or "-", **fields).log(level, message, **message_fields)


def _ensure_scraper_level() -> None:
    """Register the custom SCRAPER level if it has not already been configured."""
    try:
        logger.level("SCRAPER")
    except ValueError:
        logger.level("SCRAPER", no=25, color="<blue>", icon="")


__all__ = ["LogContext", "bind_context", "configure_logging", "log_event", "logger"]
