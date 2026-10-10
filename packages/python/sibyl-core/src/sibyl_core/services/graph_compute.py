"""Run the graph build's CPU-only steps on worker threads, off the event loop.

A cold graph build decodes, proves and compares every row of an organization
in pure Python. Run between awaits on the event loop, each of those stretches
holds the loop for its whole length, and every other request on the process
(health checks, CLI calls, MCP) waits behind it. The build keeps its queries
on the loop, where the async SDK lives, and hands each I/O-free step here.

A worker thread still runs under the GIL, but the interpreter hands the GIL
to the loop thread at every switch interval, so the loop keeps answering
while a step runs. Steps must be pure: they read inputs no coroutine mutates
while they run (callers copy or freeze shared state first) and return values
the caller applies on the loop.

The pool is the graph build's own, so a burst of cold builds never queues
the default executor's DNS lookups or password hashing behind graph CPU. Its
size is about the GIL, not throughput: Python bytecode runs one thread at a
time whatever the pool size, and every extra runnable thread is one more
contender the loop thread waits behind for the GIL. On a 1,200-row cold
build, one, two and four workers all took the same wall time, while the
loop's worst stall grew from about 14 ms to 20-38 ms to 59-70 ms. Two
workers let a second step (a concurrent build, or a large search proof)
proceed beside a long one instead of queueing behind it.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

GRAPH_COMPUTE_WORKERS = 2
# Below this many rows a step costs the loop a few milliseconds at most, less
# than queueing it behind a large build's step on the pool would.
INLINE_ROW_LIMIT = 64

_EXECUTOR = ThreadPoolExecutor(
    max_workers=GRAPH_COMPUTE_WORKERS, thread_name_prefix="sibyl-graph-compute"
)


async def off_loop[**P, R](func: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """Run ``func`` on a graph compute thread in the caller's context.

    Context variables (structlog's bound request context among them) are
    copied, so a log line emitted by the step carries the same bindings as
    one emitted on the loop.
    """
    loop = asyncio.get_running_loop()
    call = functools.partial(contextvars.copy_context().run, func, *args, **kwargs)
    return await loop.run_in_executor(_EXECUTOR, call)


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


__all__ = ["GRAPH_COMPUTE_WORKERS", "INLINE_ROW_LIMIT", "compute_rows", "off_loop"]
