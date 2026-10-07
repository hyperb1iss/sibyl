"""The local lock registry holds a key only while someone holds or waits for it."""

from __future__ import annotations

import asyncio

import pytest

from sibyl.coordination._local.locks import LocalLockManager


@pytest.mark.asyncio
async def test_local_lock_manager_forgets_a_key_once_nobody_holds_or_waits_for_it() -> None:
    """Every idempotent request brings a new key; the registry must not keep them all."""
    manager = LocalLockManager()
    for index in range(10_000):
        async with manager.lock("org_123", f"idempotency:{index}"):
            assert f"org_123:idempotency:{index}" in manager._locks
    assert manager._locks == {}
    assert manager._tokens == {}

    # A non-blocking miss leaves nothing behind either.
    async with manager.lock("org_123", "held"):
        assert await manager.acquire("org_123", "held", blocking=False) is None
        assert "org_123:held" in manager._locks
    assert manager._locks == {}

    # A waiter keeps the key alive until it has had its turn.
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with manager.lock("org_123", "contended"):
            entered.set()
            await release.wait()

    async def waiter() -> None:
        await entered.wait()
        async with manager.lock("org_123", "contended"):
            pass

    holding = asyncio.create_task(holder())
    waiting = asyncio.create_task(waiter())
    await entered.wait()
    await asyncio.sleep(0)
    assert manager._locks["org_123:contended"].waiters == 1
    release.set()
    await asyncio.gather(holding, waiting)
    assert manager._locks == {}
