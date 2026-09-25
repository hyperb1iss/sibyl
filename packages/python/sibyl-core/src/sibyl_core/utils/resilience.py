"""Timeout utilities for bounding slow async operations."""

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

import structlog

log = structlog.get_logger()

P = ParamSpec("P")
T = TypeVar("T")


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
        name = operation_name or getattr(func, "__name__", "<unknown>")

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            return await with_timeout(func(*args, **kwargs), seconds, name)

        return wrapper

    return decorator


# Timeout defaults for different operations
TIMEOUTS = {
    "graph_connect": 15.0,
    "graph_query": 60.0,  # Increased for complex queries under load
    "search": 30.0,  # Increased for fulltext search under load
    "embedding": 30.0,  # Increased for batch embeddings
    "ingestion_file": 120.0,  # Increased for large file processing
}
