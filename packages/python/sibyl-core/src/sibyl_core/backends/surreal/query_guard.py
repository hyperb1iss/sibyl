"""Task-local guards at the final prepared native query boundary."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sibyl_core.backends.surreal.protocols import QueryParams

type NativeQueryGuard = Callable[[str, QueryParams | None], Awaitable[None]]

_native_query_guard: ContextVar[NativeQueryGuard | None] = ContextVar(
    "surreal_native_query_guard", default=None
)


@contextmanager
def guard_native_query_requests(guard: NativeQueryGuard) -> Iterator[None]:
    """Bind a guard to this operation without changing a shared client's policy."""
    token = _native_query_guard.set(guard)
    try:
        yield
    finally:
        _native_query_guard.reset(token)


async def check_native_query_request(query: str, params: QueryParams | None) -> None:
    """Check the exact query after namespace preparation and before SDK dispatch."""
    if (guard := _native_query_guard.get()) is not None:
        await guard(query, params)
