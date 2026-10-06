"""Compatibility shim for coordination lock backends."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, Callable
from functools import wraps
from typing import Any

from sibyl.coordination._local.locks import LocalLockManager
from sibyl.coordination._redis.locks import EntityLockManager
from sibyl.coordination.locks import (
    LOCK_MAX_RETRIES,
    LOCK_RETRY_DELAY_BASE,
    LOCK_TTL_SECONDS,
    LOCK_WAIT_TIMEOUT,
    LockAcquisitionError,
    LockBackend,
    get_locks,
)

__all__ = [
    "EntityLockManager",
    "LOCK_MAX_RETRIES",
    "LOCK_RETRY_DELAY_BASE",
    "LOCK_TTL_SECONDS",
    "LOCK_WAIT_TIMEOUT",
    "LocalLockManager",
    "LockAcquisitionError",
    "entity_lock",
    "get_lock_manager",
    "init_locks",
    "shutdown_locks",
    "with_entity_lock",
]


def get_lock_manager() -> LockBackend:
    """Return the active lock backend."""
    return get_locks()


async def init_locks() -> None:
    """Initialize the active lock backend on server startup."""
    manager = get_lock_manager()
    await manager.connect()


async def shutdown_locks() -> None:
    """Shutdown the active lock backend on server shutdown."""
    manager = get_lock_manager()
    await manager.disconnect()


@contextlib.asynccontextmanager
async def entity_lock(
    org_id: str,
    entity_id: str,
    wait_timeout: float = LOCK_WAIT_TIMEOUT,
    blocking: bool = True,
) -> AsyncGenerator[str | None]:
    """Convenience context manager for entity locking."""
    manager = get_lock_manager()
    async with manager.lock(org_id, entity_id, wait_timeout, blocking) as token:
        yield token


def with_entity_lock(*, org_id_arg: str = "org_id", entity_id_arg: str = "entity_id"):
    """Decorator to wrap a function with entity locking."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            org_id = kwargs.get(org_id_arg)
            entity_id = kwargs.get(entity_id_arg)
            if org_id is None or entity_id is None:
                raise ValueError(
                    f"with_entity_lock requires '{org_id_arg}' and '{entity_id_arg}' kwargs"
                )

            async with entity_lock(org_id, entity_id):
                return await func(*args, **kwargs)

        return wrapper

    return decorator
