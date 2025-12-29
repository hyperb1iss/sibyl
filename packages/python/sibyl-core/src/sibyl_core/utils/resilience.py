"""Resilience utilities for handling transient failures."""

import asyncio
import functools
import random
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

import structlog

log = structlog.get_logger()

P = ParamSpec("P")
T = TypeVar("T")


class RetryConfig:
    """Configuration for retry behavior."""

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 0.5,
        max_delay: float = 10.0,
        exponential_base: float = 2.0,
        jitter: bool = True,
        retryable_exceptions: tuple[type[Exception], ...] = (
            ConnectionError,
            TimeoutError,
            OSError,
        ),
    ) -> None:
        """Initialize retry configuration.

        Args:
            max_attempts: Maximum number of attempts (including first try)
            base_delay: Initial delay between retries in seconds
            max_delay: Maximum delay between retries
            exponential_base: Base for exponential backoff
            jitter: Add random jitter to delays
            retryable_exceptions: Tuple of exception types to retry on
        """
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter
        self.retryable_exceptions = retryable_exceptions


# Default configurations for different scenarios
GRAPH_RETRY = RetryConfig(
    max_attempts=3,
    base_delay=0.5,
    max_delay=5.0,
    retryable_exceptions=(ConnectionError, TimeoutError, OSError),
)

SEARCH_RETRY = RetryConfig(
    max_attempts=2,
    base_delay=0.3,
    max_delay=2.0,
    retryable_exceptions=(ConnectionError, TimeoutError),
)


def calculate_delay(attempt: int, config: RetryConfig) -> float:
    """Calculate delay for a given attempt with exponential backoff.

    Args:
        attempt: Current attempt number (0-indexed)
        config: Retry configuration

    Returns:
        Delay in seconds
    """
    delay = config.base_delay * (config.exponential_base**attempt)
    delay = min(delay, config.max_delay)

    if config.jitter:
        # Add up to 25% jitter (non-cryptographic, just for retry backoff)
        jitter_range = delay * 0.25
        delay += random.uniform(-jitter_range, jitter_range)

    return max(0.0, delay)


def retry(
    config: RetryConfig | None = None,
    on_retry: Callable[[int, Exception], None] | None = None,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Decorator for adding retry logic to async functions.

    Args:
        config: Retry configuration (defaults to GRAPH_RETRY)
        on_retry: Optional callback called on each retry with (attempt, exception)

    Returns:
        Decorated function with retry logic

    Example:
        @retry(config=GRAPH_RETRY)
        async def fetch_data():
            ...
    """
    if config is None:
        config = GRAPH_RETRY

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            last_exception: Exception | None = None

            for attempt in range(config.max_attempts):
                try:
                    return await func(*args, **kwargs)
                except config.retryable_exceptions as e:
                    last_exception = e

                    if attempt < config.max_attempts - 1:
                        delay = calculate_delay(attempt, config)
                        log.warning(
                            "Retrying after transient failure",
                            function=func.__name__,
                            attempt=attempt + 1,
                            max_attempts=config.max_attempts,
                            delay=f"{delay:.2f}s",
                            error=str(e),
                        )

                        if on_retry:
                            on_retry(attempt + 1, e)

                        await asyncio.sleep(delay)
                    else:
                        # Use log.error (not exception) to avoid traceback spam
                        log.error(
                            "All retry attempts exhausted",
                            function=func.__name__,
                            attempts=config.max_attempts,
                            error=str(e),
                        )

            # Should never reach here, but satisfy type checker
            if last_exception:
                raise last_exception
            raise RuntimeError("Retry logic error")

        return wrapper

    return decorator


async def with_timeout[R](
    coro: Awaitable[R],
    timeout_seconds: float,
    operation_name: str = "operation",
) -> R:
    """Execute a coroutine with a timeout.

    Args:
        coro: Coroutine to execute
        timeout_seconds: Timeout in seconds
        operation_name: Name of operation for error messages

    Returns:
        Result of the coroutine

    Raises:
        TimeoutError: If operation times out
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except TimeoutError as e:
        # Use log.error (not exception) to avoid traceback spam
        log.error(
            "Operation timed out",
            operation=operation_name,
            timeout=f"{timeout_seconds}s",
        )
        raise TimeoutError(f"{operation_name} timed out after {timeout_seconds}s") from e


def timeout(
    seconds: float,
    operation_name: str | None = None,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Decorator for adding timeout to async functions.

    Args:
        seconds: Timeout in seconds
        operation_name: Name for error messages (defaults to function name)

    Returns:
        Decorated function with timeout

    Example:
        @timeout(5.0)
        async def slow_operation():
            ...
    """

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        name = operation_name or func.__name__

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            return await with_timeout(func(*args, **kwargs), seconds, name)

        return wrapper

    return decorator


# Timeout defaults for different operations
TIMEOUTS = {
    "graph_connect": 10.0,
    "graph_query": 30.0,
    "search": 15.0,
    "embedding": 20.0,
    "ingestion_file": 60.0,
}
