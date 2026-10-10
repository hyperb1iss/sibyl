"""Keep MCP write tools from stopping part way through."""

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps

import anyio
import structlog

log = structlog.get_logger()


def uninterruptible[**P, R](tool: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Run a write tool to completion even when its caller goes away.

    The streamable HTTP transport ties a call to its response stream, so a
    client that disconnects or cancels mid-call cancels the tool. A write that
    spans several stores (raw memory, graph entity, links, idempotency receipt)
    would stop between them. The write runs as its own task and the caller
    waits it out under a shield, so it lands whole; the cancellation is raised
    once it has.
    """

    @wraps(tool)
    async def run(*args: P.args, **kwargs: P.kwargs) -> R:
        write = asyncio.ensure_future(tool(*args, **kwargs))
        try:
            return await asyncio.shield(write)
        except asyncio.CancelledError as cancellation:
            # Shield from ASGI scope cancellation and explicit Task.cancel().
            with anyio.CancelScope(shield=True):
                while not write.done():
                    try:
                        await asyncio.shield(write)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
            cause = None if write.cancelled() else write.exception()
            if cause is not None:
                # Nobody is left to receive this error, so record it here.
                log.exception(
                    "mcp_write_failed_after_caller_left",
                    tool=getattr(tool, "__name__", repr(tool)),
                    error=str(cause),
                    exc_info=(type(cause), cause, cause.__traceback__),
                )
            raise cancellation from cause

    return run
