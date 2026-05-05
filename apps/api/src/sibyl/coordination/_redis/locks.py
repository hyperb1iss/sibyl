"""Redis lock backend."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable
from typing import cast

import structlog
from redis.asyncio import Redis

from sibyl.config import settings
from sibyl.coordination.locks import (
    LOCK_MAX_RETRIES,
    LOCK_RETRY_DELAY_BASE,
    LOCK_TTL_SECONDS,
    LOCK_WAIT_TIMEOUT,
    LockAcquisitionError,
)

log = structlog.get_logger()

LOCKS_DB = 3


class EntityLockManager:
    """Redis-based distributed lock manager for entity updates."""

    def __init__(self) -> None:
        self._redis: Redis | None = None
        self._lock_id = str(uuid.uuid4())[:8]

    async def connect(self) -> None:
        """Connect to Redis for locking."""
        if self._redis is not None:
            return

        redis_host = settings.redis_host or "127.0.0.1"
        redis_port = settings.redis_port or 6381

        self._redis = Redis(
            host=redis_host,
            port=redis_port,
            password=settings.redis_password_value or None,
            db=LOCKS_DB,
            decode_responses=True,
        )

        await self._redis.ping()
        log.info(
            "entity_lock_manager_connected",
            host=redis_host,
            port=redis_port,
            db=LOCKS_DB,
        )

    async def disconnect(self) -> None:
        """Disconnect from Redis."""
        if self._redis:
            await self._redis.close()
            self._redis = None
            log.info("entity_lock_manager_disconnected")

    def _lock_key(self, org_id: str, entity_id: str) -> str:
        return f"sibyl:lock:{org_id}:{entity_id}"

    def _lock_value(self) -> str:
        return f"{self._lock_id}:{time.time()}"

    async def acquire(
        self,
        org_id: str,
        entity_id: str,
        wait_timeout: float = LOCK_WAIT_TIMEOUT,
        blocking: bool = True,
    ) -> str | None:
        """Acquire a distributed lock on an entity."""
        if self._redis is None:
            await self.connect()

        key = self._lock_key(org_id, entity_id)
        value = self._lock_value()
        start_time = time.time()
        retries = 0

        while True:
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

            elapsed = time.time() - start_time
            if elapsed >= wait_timeout:
                log.warning(
                    "entity_lock_timeout",
                    entity_id=entity_id,
                    org_id=org_id,
                    elapsed=elapsed,
                )
                raise LockAcquisitionError(entity_id, org_id, "timeout")

            retries += 1
            if retries > LOCK_MAX_RETRIES:
                ttl = await self._redis.ttl(key)
                if ttl == -1:
                    await self._redis.expire(key, LOCK_TTL_SECONDS)
                    log.warning("entity_lock_repaired_ttl", entity_id=entity_id)

            delay = LOCK_RETRY_DELAY_BASE * (2 ** min(retries, 4))
            delay += asyncio.get_event_loop().time() % 0.1
            await asyncio.sleep(delay)

    async def release(self, org_id: str, entity_id: str, token: str) -> bool:
        """Release a lock if this process owns it."""
        if self._redis is None:
            return False

        key = self._lock_key(org_id, entity_id)
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """

        released = await cast("Awaitable[object]", self._redis.eval(lua_script, 1, key, token))

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
        """Extend a lock TTL when this process owns it."""
        if self._redis is None:
            return False

        key = self._lock_key(org_id, entity_id)
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("expire", KEYS[1], ARGV[2])
        else
            return 0
        end
        """

        extended = await cast(
            "Awaitable[object]",
            self._redis.eval(lua_script, 1, key, token, LOCK_TTL_SECONDS),
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
        """Context manager for entity locking."""
        token = await self.acquire(org_id, entity_id, wait_timeout, blocking)
        try:
            yield token
        finally:
            if token:
                await self.release(org_id, entity_id, token)
