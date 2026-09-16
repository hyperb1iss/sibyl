"""Pool warming shared by the process-wide Surreal singletons."""

from __future__ import annotations

from typing import Protocol

import structlog

log = structlog.get_logger()


class _WarmablePool(Protocol):
    async def warm_pool(self) -> None: ...


async def warm_shared_pool(client: _WarmablePool, *, client_kind: str) -> bool:
    """Open every socket in a freshly built pool, reporting whether it worked.

    Warming on the build keeps the handshake off the first request. A failure
    is not fatal: the pool reconnects lazily, so the caller keeps the client
    and the failure is named instead of hidden.
    """
    try:
        await client.warm_pool()
    except Exception as exc:
        log.warning(
            "shared_surreal_pool_warm_failed",
            client=client_kind,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False
    log.info("shared_surreal_pool_warmed", client=client_kind)
    return True


__all__ = ["warm_shared_pool"]
