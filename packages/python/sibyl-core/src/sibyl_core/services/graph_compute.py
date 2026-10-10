"""Run CPU-only graph proof steps on worker threads, off the event loop.

A cold graph build decodes, proves and compares every row of an organization
in pure Python. Run between awaits on the event loop, each of those stretches
holds the loop for its whole length, and every other request on the process
(health checks, CLI calls, MCP) waits behind it. Callers keep their queries
on the loop, where the async SDK lives, and hand each I/O-free step here.

A worker thread still runs under the GIL, but the interpreter hands the GIL
to the loop thread at every switch interval, so the loop keeps answering
while a step runs. Steps must be pure: they read inputs no coroutine mutates
while they run (callers copy or freeze shared state first) and return values
the caller applies on the loop.

There are two pools. A whole-graph build marks its context with
graph_build_steps(), and its steps (over every row of an organization, a
second or more each on a large graph) run on the build pool. Every other
step, a search proof or a get_many batch, runs on the request pool, so it
never queues behind a build. Neither pool is shared with the default
executor, so builds never queue DNS lookups or password hashing either.

Pool sizes are about the GIL, not throughput: Python bytecode runs one
thread at a time whatever the size, and every runnable thread is one more
contender the loop thread waits behind. On a 1,200-row cold build, one, two
and four build workers took the same wall time while the loop's worst stall
grew from about 14 ms to 20-38 ms to 59-70 ms. Two build workers let two
readers' builds interleave their steps instead of queueing.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from collections.abc import Callable, Coroutine, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

GRAPH_BUILD_WORKERS = 2
REQUEST_WORKERS = 2
# Below this many rows a step costs the loop a few milliseconds at most, less
# than handing it to a thread would.
INLINE_ROW_LIMIT = 64

_GRAPH_BUILD = "graph-build"
_REQUEST = "request"

_in_graph_build: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "sibyl_graph_build_steps", default=False
)
_pools: dict[str, ThreadPoolExecutor] = {}
_pools_lock = threading.Lock()


@contextmanager
def graph_build_steps() -> Iterator[None]:
    """Run the compute steps started in this context on the graph build pool.

    Tasks created inside inherit the mark, so a build's gathered batches
    stay on its pool too.
    """
    token = _in_graph_build.set(True)
    try:
        yield
    finally:
        _in_graph_build.reset(token)


def graph_build[**P, R](
    func: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """Mark an async whole-graph build: its steps run on the graph build pool."""

    @functools.wraps(func)
    async def build(*args: P.args, **kwargs: P.kwargs) -> R:
        with graph_build_steps():
            return await func(*args, **kwargs)

    return build


def _pool(kind: str) -> ThreadPoolExecutor:
    with _pools_lock:
        pool = _pools.get(kind)
        if pool is None:
            pool = ThreadPoolExecutor(
                max_workers=GRAPH_BUILD_WORKERS if kind == _GRAPH_BUILD else REQUEST_WORKERS,
                thread_name_prefix=f"sibyl-{kind}-compute",
            )
            _pools[kind] = pool
        return pool


async def off_loop[**P, R](func: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """Run ``func`` on a compute thread in the caller's context.

    Context variables (structlog's bound request context among them) are
    copied, so a log line emitted by the step carries the same bindings as
    one emitted on the loop.
    """
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    pool = _pool(_GRAPH_BUILD if context.get(_in_graph_build) else _REQUEST)
    call = functools.partial(context.run, func, *args, **kwargs)
    return await loop.run_in_executor(pool, call)


async def compute_rows[**P, R](
    rows: int, func: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
) -> R:
    """Run a step over ``rows`` rows inline when it is small, off the loop otherwise.

    Shared proof helpers serve one-row lookups as well as whole-graph
    builds; the small calls keep their latency and the large ones keep the
    loop free. Either way the step is the same pure function.
    """
    if rows < INLINE_ROW_LIMIT:
        return func(*args, **kwargs)
    return await off_loop(func, *args, **kwargs)


def shutdown_compute_pools() -> None:
    """Stop both pools at process shutdown.

    Queued steps are cancelled and running ones finish on their own; their
    callers are shutting down too. A step started afterwards (a test that
    runs several lifespans in one process) gets fresh pools.
    """
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.shutdown(wait=False, cancel_futures=True)


__all__ = [
    "GRAPH_BUILD_WORKERS",
    "INLINE_ROW_LIMIT",
    "REQUEST_WORKERS",
    "compute_rows",
    "graph_build",
    "graph_build_steps",
    "off_loop",
    "shutdown_compute_pools",
]
