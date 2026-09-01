"""Reusable retry decorator with exponential backoff and logging."""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from utils.logger import logger

P = ParamSpec("P")
R = TypeVar("R")


def retry(
    max_retries: int = 3,
    delay_seconds: float = 1.0,
    backoff_factor: float = 2.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Retry a callable when configured exceptions are raised."""
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")
    if delay_seconds < 0:
        raise ValueError("delay_seconds must be non-negative")
    if backoff_factor < 1:
        raise ValueError("backoff_factor must be at least 1")

    def decorator(function: Callable[P, R]) -> Callable[P, R]:
        """Wrap the target function with retry behavior."""

        @wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            """Execute the wrapped function with retries."""
            attempts = max_retries + 1
            current_delay = delay_seconds
            for attempt in range(1, attempts + 1):
                try:
                    return function(*args, **kwargs)
                except exceptions as exc:
                    if attempt == attempts:
                        logger.error(
                            "Function {name} failed after {attempts} attempts: {error}",
                            name=function.__name__,
                            attempts=attempts,
                            error=exc,
                        )
                        raise
                    logger.warning(
                        "Function {name} failed on attempt {attempt}/{attempts}: {error}. "
                        "Retrying in {delay:.2f}s",
                        name=function.__name__,
                        attempt=attempt,
                        attempts=attempts,
                        error=exc,
                        delay=current_delay,
                    )
                    time.sleep(current_delay)
                    current_delay *= backoff_factor
            raise RuntimeError("Unreachable retry state")

        return wrapper

    return decorator
