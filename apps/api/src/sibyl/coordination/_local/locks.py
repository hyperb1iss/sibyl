"""Local in-process lock backend."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

from sibyl.coordination.locks import LOCK_WAIT_TIMEOUT, LockAcquisitionError


@dataclass(slots=True)
class _Entry:
    """One key's lock and how many callers are between claiming it and holding it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    waiters: int = 0


class LocalLockManager:
    """Serialize entity writes within a single process.

    A key lives in the registry only while someone holds or waits for its
    lock. Every idempotent request and every task update brings a new key,
    so a registry that never forgot one grew by a lock per request for the
    life of the process.
    """

    def __init__(self) -> None:
        self._locks: dict[str, _Entry] = {}
        self._tokens: dict[str, str] = {}
        self._registry_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Initialize the local lock manager."""

    async def disconnect(self) -> None:
        """Reset local lock state on shutdown."""
        async with self._registry_lock:
            self._locks = {key: entry for key, entry in self._locks.items() if entry.lock.locked()}
            self._tokens.clear()

    def _lock_key(self, org_id: str, entity_id: str) -> str:
        return f"{org_id}:{entity_id}"

    async def _claim(self, key: str) -> _Entry:
        """Fetch or create the key's entry and count the caller as waiting on it."""
        async with self._registry_lock:
            entry = self._locks.setdefault(key, _Entry())
            entry.waiters += 1
            return entry

    def _unclaim(self, key: str, entry: _Entry) -> None:
        """Stop waiting; a key nobody holds or waits for leaves the registry."""
        entry.waiters -= 1
        self._forget_if_idle(key, entry)

    def _forget_if_idle(self, key: str, entry: _Entry) -> None:
        if entry.waiters == 0 and not entry.lock.locked() and self._locks.get(key) is entry:
            del self._locks[key]

    async def acquire(
        self,
        org_id: str,
        entity_id: str,
        wait_timeout: float = LOCK_WAIT_TIMEOUT,
        blocking: bool = True,
    ) -> str | None:
        """Acquire an in-process lock."""
        key = self._lock_key(org_id, entity_id)
        entry = await self._claim(key)
        try:
            if not blocking:
                if entry.lock.locked():
                    return None
                await entry.lock.acquire()
            else:
                try:
                    await asyncio.wait_for(entry.lock.acquire(), timeout=wait_timeout)
                except TimeoutError as exc:
                    raise LockAcquisitionError(entity_id, org_id, "timeout") from exc
        finally:
            # A caller that now holds the lock keeps the entry alive through
            # it; one that gave up may have been the last to care about it.
            self._unclaim(key, entry)

        token = f"local:{uuid.uuid4().hex[:8]}"
        self._tokens[key] = token
        return token

    async def release(self, org_id: str, entity_id: str, token: str) -> bool:
        """Release a lock if the token matches the current holder."""
        key = self._lock_key(org_id, entity_id)
        entry = self._locks.get(key)
        if entry is None or self._tokens.get(key) != token:
            return False

        self._tokens.pop(key, None)
        if entry.lock.locked():
            entry.lock.release()
        self._forget_if_idle(key, entry)
        return True

    async def extend(self, org_id: str, entity_id: str, token: str) -> bool:
        """Extend is a no-op in local mode while the process is alive."""
        key = self._lock_key(org_id, entity_id)
        return self._tokens.get(key) == token

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
