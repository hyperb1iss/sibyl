"""Distributed locking for entity updates.

Provides Redis-based distributed locks to prevent concurrent writes
to the same entity from corrupting data. Uses a short TTL to recover
from dead processes while allowing blocking updates.

Architecture:
    Agent A (update entity X) -> acquire lock -> perform update -> release lock
    Agent B (update entity X) -> wait for lock -> acquire -> perform update -> release

Lock keys: sibyl:lock:{org_id}:{entity_id}
Lock TTL: 10 seconds (auto-expires for recovery)
"""

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import structlog
from redis.asyncio import Redis

from sibyl.config import settings

log = structlog.get_logger()

# Use dedicated Redis database for locks (separate from graph/jobs/pubsub)
LOCKS_DB = 3

# Lock configuration
LOCK_TTL_SECONDS = 30  # Auto-expire for recovery (Graphiti can take 20+ seconds)
LOCK_RETRY_DELAY_BASE = 0.2  # Base delay between retries (exponential backoff)
LOCK_MAX_RETRIES = 10  # Maximum retry attempts
LOCK_WAIT_TIMEOUT = 45.0  # Maximum time to wait for lock (slightly longer than TTL)


class LockAcquisitionError(Exception):
    """Failed to acquire entity lock."""

    def __init__(self, entity_id: str, org_id: str, reason: str = "timeout"):
        self.entity_id = entity_id
        self.org_id = org_id
        self.reason = reason
        super().__init__(f"Failed to acquire lock for {entity_id}: {reason}")


class EntityLockManager:
    """Redis-based distributed lock manager for entity updates."""

    def __init__(self) -> None:
        self._redis: Redis | None = None
        self._lock_id = str(uuid.uuid4())[:8]  # Unique identifier for this instance

    async def connect(self) -> None:
        """Connect to Redis for locking."""
        if self._redis is not None:
            return

        self._redis = Redis(
            host=settings.falkordb_host,
            port=settings.falkordb_port,
            password=settings.falkordb_password,
            db=LOCKS_DB,
            decode_responses=True,
        )

        # Test connection
        await self._redis.ping()
        log.info(
            "entity_lock_manager_connected",
            host=settings.falkordb_host,
            port=settings.falkordb_port,
            db=LOCKS_DB,
        )

    async def disconnect(self) -> None:
        """Disconnect from Redis."""
        if self._redis:
            await self._redis.close()
            self._redis = None
            log.info("entity_lock_manager_disconnected")

    def _lock_key(self, org_id: str, entity_id: str) -> str:
        """Generate lock key for an entity."""
        return f"sibyl:lock:{org_id}:{entity_id}"

    def _lock_value(self) -> str:
        """Generate unique lock value (instance_id:timestamp)."""
        return f"{self._lock_id}:{time.time()}"

    async def acquire(
        self,
        org_id: str,
        entity_id: str,
        wait_timeout: float = LOCK_WAIT_TIMEOUT,
        blocking: bool = True,
    ) -> str | None:
        """Acquire a lock on an entity.

        Args:
            org_id: Organization UUID
            entity_id: Entity UUID
            wait_timeout: Maximum time to wait for lock
            blocking: If False, return immediately if lock not available

        Returns:
            Lock token if acquired, None if not (non-blocking only)

        Raises:
            LockAcquisitionError: If blocking and lock cannot be acquired
        """
        if self._redis is None:
            await self.connect()

        key = self._lock_key(org_id, entity_id)
        value = self._lock_value()
        start_time = time.time()
        retries = 0

        while True:
            # Try to acquire lock with SET NX (only if not exists) and TTL
            acquired = await self._redis.set(  # type: ignore[union-attr]
                key, value, nx=True, ex=LOCK_TTL_SECONDS
            )

            if acquired:
                log.debug(
                    "entity_lock_acquired",
                    entity_id=entity_id,
                    org_id=org_id,
                    lock_token=value,
                )
                return value

            if not blocking:
                return None

            # Check wait_timeout
            elapsed = time.time() - start_time
            if elapsed >= wait_timeout:
                log.warning(
                    "entity_lock_timeout",
                    entity_id=entity_id,
                    org_id=org_id,
                    elapsed=elapsed,
                )
                raise LockAcquisitionError(entity_id, org_id, "timeout")

            # Exponential backoff with jitter
            retries += 1
            if retries > LOCK_MAX_RETRIES:
                # Check if lock is stale (holder died)
                ttl = await self._redis.ttl(key)
                if ttl == -1:  # No expiry - shouldn't happen but recover
                    await self._redis.expire(key, LOCK_TTL_SECONDS)
                    log.warning("entity_lock_repaired_ttl", entity_id=entity_id)

            delay = LOCK_RETRY_DELAY_BASE * (2 ** min(retries, 4))  # Cap at 1.6s
            delay += asyncio.get_event_loop().time() % 0.1  # Add jitter
            await asyncio.sleep(delay)

    async def release(self, org_id: str, entity_id: str, token: str) -> bool:
        """Release a lock on an entity.

        Args:
            org_id: Organization UUID
            entity_id: Entity UUID
            token: Lock token returned from acquire()

        Returns:
            True if lock was released, False if lock was held by another process
        """
        if self._redis is None:
            return False

        key = self._lock_key(org_id, entity_id)

        # Only release if we hold the lock (compare-and-delete)
        # Use Lua script for atomicity
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """

        released = await self._redis.eval(lua_script, 1, key, token)  # type: ignore[union-attr]

        if released:
            log.debug("entity_lock_released", entity_id=entity_id, org_id=org_id)
        else:
            log.warning(
                "entity_lock_release_failed",
                entity_id=entity_id,
                org_id=org_id,
                reason="not_owner",
            )

        return bool(released)

    async def extend(self, org_id: str, entity_id: str, token: str) -> bool:
        """Extend a lock's TTL.

        Useful for long-running operations that need more time.

        Args:
            org_id: Organization UUID
            entity_id: Entity UUID
            token: Lock token returned from acquire()

        Returns:
            True if lock was extended, False otherwise
        """
        if self._redis is None:
            return False

        key = self._lock_key(org_id, entity_id)

        # Only extend if we hold the lock
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("expire", KEYS[1], ARGV[2])
        else
            return 0
        end
        """

        extended = await self._redis.eval(  # type: ignore[union-attr]
            lua_script, 1, key, token, LOCK_TTL_SECONDS
        )

        if extended:
            log.debug("entity_lock_extended", entity_id=entity_id, org_id=org_id)

        return bool(extended)

    @contextlib.asynccontextmanager
    async def lock(
        self,
        org_id: str,
        entity_id: str,
        wait_timeout: float = LOCK_WAIT_TIMEOUT,
        blocking: bool = True,
    ) -> AsyncGenerator[str | None]:
        """Context manager for entity locking.

        Example:
            async with lock_manager.lock(org_id, entity_id) as token:
                if token:  # Lock acquired
                    await update_entity(...)

        Args:
            org_id: Organization UUID
            entity_id: Entity UUID
            wait_timeout: Maximum time to wait for lock
            blocking: If False, yield None if lock not available

        Yields:
            Lock token if acquired, None if not (non-blocking only)
        """
        token = await self.acquire(org_id, entity_id, wait_timeout, blocking)
        try:
            yield token
        finally:
            if token:
                await self.release(org_id, entity_id, token)


# Global lock manager instance
_lock_manager: EntityLockManager | None = None


def get_lock_manager() -> EntityLockManager:
    """Get or create the global lock manager."""
    global _lock_manager  # noqa: PLW0603
    if _lock_manager is None:
        _lock_manager = EntityLockManager()
    return _lock_manager


async def init_locks() -> None:
    """Initialize lock manager on server startup."""
    manager = get_lock_manager()
    await manager.connect()


async def shutdown_locks() -> None:
    """Shutdown lock manager on server shutdown."""
    manager = get_lock_manager()
    await manager.disconnect()


# Convenience function for simple locking
@contextlib.asynccontextmanager
async def entity_lock(
    org_id: str,
    entity_id: str,
    wait_timeout: float = LOCK_WAIT_TIMEOUT,
    blocking: bool = True,
) -> AsyncGenerator[str | None]:
    """Convenience context manager for entity locking.

    Example:
        async with entity_lock(org_id, entity_id) as token:
            if token:
                await do_update()

    Args:
        org_id: Organization UUID
        entity_id: Entity UUID
        wait_timeout: Maximum time to wait for lock
        blocking: If False, yield None if lock not available

    Yields:
        Lock token if acquired, None if not (non-blocking only)
    """
    manager = get_lock_manager()
    async with manager.lock(org_id, entity_id, wait_timeout, blocking) as token:
        yield token


def with_entity_lock(*, org_id_arg: str = "org_id", entity_id_arg: str = "entity_id"):
    """Decorator to wrap a function with entity locking.

    The decorated function must have org_id and entity_id as keyword arguments.

    Example:
        @with_entity_lock()
        async def update_task(org_id: str, entity_id: str, updates: dict):
            ...

        @with_entity_lock(org_id_arg="organization_id", entity_id_arg="task_id")
        async def update_task(organization_id: str, task_id: str, updates: dict):
            ...

    Args:
        org_id_arg: Name of the org_id argument in the wrapped function
        entity_id_arg: Name of the entity_id argument in the wrapped function
    """

    def decorator(func: Any) -> Any:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            org_id = kwargs.get(org_id_arg)
            entity_id = kwargs.get(entity_id_arg)

            if not org_id or not entity_id:
                raise ValueError(
                    f"with_entity_lock requires '{org_id_arg}' and '{entity_id_arg}' kwargs"
                )

            async with entity_lock(org_id, entity_id):
                return await func(*args, **kwargs)

        return wrapper

    return decorator
