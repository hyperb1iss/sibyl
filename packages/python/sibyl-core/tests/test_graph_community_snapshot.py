"""Concurrency contracts for shared community graph snapshot loads."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sibyl_core.services import graph_community_snapshot as snapshots
from sibyl_core.services.graph_community_models import GraphSnapshot

type _SnapshotKey = tuple[str, int | None, int | None]


def _empty_snapshot() -> GraphSnapshot:
    return GraphSnapshot(entities=[], relationships=[], entity_by_id={})


def _reset_snapshot_state() -> None:
    assert not snapshots.GRAPH_SNAPSHOT_LOADS
    assert not snapshots._GRAPH_SNAPSHOT_WAITERS
    snapshots.GRAPH_SNAPSHOT_CACHE.clear()


async def _wait_for_waiters(cache_key: _SnapshotKey, expected: int) -> None:
    for _ in range(10):
        loader = snapshots.GRAPH_SNAPSHOT_LOADS.get(cache_key)
        if loader is not None and snapshots._GRAPH_SNAPSHOT_WAITERS.get(loader) == expected:
            return
        await asyncio.sleep(0)
    loader = snapshots.GRAPH_SNAPSHOT_LOADS.get(cache_key)
    pytest.fail(
        f"snapshot waiter count never reached {expected}: "
        f"{snapshots._GRAPH_SNAPSHOT_WAITERS.get(loader) if loader else None}"
    )


@pytest.mark.asyncio
async def test_first_snapshot_caller_can_cancel_while_joiner_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_snapshot_state()
    cache_key = ("org-owner-cancel", 100, 200)
    started = asyncio.Event()
    release = asyncio.Event()
    loader_cancelled = asyncio.Event()

    async def load(*args: Any, **kwargs: Any) -> GraphSnapshot:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            loader_cancelled.set()
            raise
        return _empty_snapshot()

    monkeypatch.setattr(snapshots, "_load_graph_snapshot", load)
    first = asyncio.create_task(
        snapshots._get_graph_snapshot(
            object(),
            cache_key[0],
            max_entities=cache_key[1],
            max_relationships=cache_key[2],
        )
    )
    await started.wait()
    joiner = asyncio.create_task(
        snapshots._get_graph_snapshot(
            object(),
            cache_key[0],
            max_entities=cache_key[1],
            max_relationships=cache_key[2],
        )
    )
    await _wait_for_waiters(cache_key, 2)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert not loader_cancelled.is_set()
    loader = snapshots.GRAPH_SNAPSHOT_LOADS[cache_key]
    assert snapshots._GRAPH_SNAPSHOT_WAITERS[loader] == 1

    release.set()
    assert await joiner == _empty_snapshot()
    assert snapshots.GRAPH_SNAPSHOT_LOADS == {}
    assert snapshots._GRAPH_SNAPSHOT_WAITERS == {}


@pytest.mark.asyncio
async def test_final_snapshot_waiter_cancellation_stops_and_cleans_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_snapshot_state()
    cache_key = ("org-all-cancel", 100, 200)
    started = asyncio.Event()
    loader_cancelled = asyncio.Event()

    async def load(*args: Any, **kwargs: Any) -> GraphSnapshot:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            loader_cancelled.set()
            raise

    monkeypatch.setattr(snapshots, "_load_graph_snapshot", load)
    first = asyncio.create_task(
        snapshots._get_graph_snapshot(
            object(),
            cache_key[0],
            max_entities=cache_key[1],
            max_relationships=cache_key[2],
        )
    )
    await started.wait()
    second = asyncio.create_task(
        snapshots._get_graph_snapshot(
            object(),
            cache_key[0],
            max_entities=cache_key[1],
            max_relationships=cache_key[2],
        )
    )
    await _wait_for_waiters(cache_key, 2)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not loader_cancelled.is_set()

    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second

    assert loader_cancelled.is_set()
    assert snapshots.GRAPH_SNAPSHOT_LOADS == {}
    assert snapshots._GRAPH_SNAPSHOT_WAITERS == {}
    assert cache_key not in snapshots.GRAPH_SNAPSHOT_CACHE


@pytest.mark.asyncio
async def test_snapshot_loader_exception_cleans_every_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_snapshot_state()
    cache_key = ("org-load-error", 100, 200)
    started = asyncio.Event()
    release = asyncio.Event()
    load_count = 0

    class SnapshotFailure(RuntimeError):
        pass

    async def load(*args: Any, **kwargs: Any) -> GraphSnapshot:
        nonlocal load_count
        load_count += 1
        started.set()
        await release.wait()
        raise SnapshotFailure("load failed")

    monkeypatch.setattr(snapshots, "_load_graph_snapshot", load)
    first = asyncio.create_task(
        snapshots._get_graph_snapshot(
            object(),
            cache_key[0],
            max_entities=cache_key[1],
            max_relationships=cache_key[2],
        )
    )
    await started.wait()
    second = asyncio.create_task(
        snapshots._get_graph_snapshot(
            object(),
            cache_key[0],
            max_entities=cache_key[1],
            max_relationships=cache_key[2],
        )
    )
    await _wait_for_waiters(cache_key, 2)

    release.set()
    failures = await asyncio.gather(first, second, return_exceptions=True)

    assert load_count == 1
    assert all(isinstance(failure, SnapshotFailure) for failure in failures)
    assert snapshots.GRAPH_SNAPSHOT_LOADS == {}
    assert snapshots._GRAPH_SNAPSHOT_WAITERS == {}
    assert cache_key not in snapshots.GRAPH_SNAPSHOT_CACHE


@pytest.mark.asyncio
async def test_snapshot_load_retries_successfully_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_snapshot_state()
    cache_key = ("org-load-retry", 100, 200)
    load_count = 0
    expected = _empty_snapshot()

    async def load(*args: Any, **kwargs: Any) -> GraphSnapshot:
        nonlocal load_count
        load_count += 1
        if load_count == 1:
            raise RuntimeError("transient load failure")
        snapshots.GRAPH_SNAPSHOT_CACHE[cache_key] = (
            datetime.now(UTC),
            expected,
        )
        return expected

    monkeypatch.setattr(snapshots, "_load_graph_snapshot", load)

    with pytest.raises(RuntimeError, match="transient load failure"):
        await snapshots._get_graph_snapshot(
            object(),
            cache_key[0],
            max_entities=cache_key[1],
            max_relationships=cache_key[2],
        )

    assert snapshots.GRAPH_SNAPSHOT_LOADS == {}
    assert snapshots._GRAPH_SNAPSHOT_WAITERS == {}

    retried = await snapshots._get_graph_snapshot(
        object(),
        cache_key[0],
        max_entities=cache_key[1],
        max_relationships=cache_key[2],
    )

    assert retried is expected
    assert load_count == 2
    assert snapshots.GRAPH_SNAPSHOT_CACHE[cache_key][1] is expected
    assert snapshots.GRAPH_SNAPSHOT_LOADS == {}
    assert snapshots._GRAPH_SNAPSHOT_WAITERS == {}


@pytest.mark.asyncio
async def test_expired_snapshot_cache_refreshes_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_snapshot_state()
    cache_key = ("org-cache-refresh", 100, 200)
    cache = snapshots.GRAPH_SNAPSHOT_CACHE
    stale = _empty_snapshot()
    fresh = _empty_snapshot()
    cache[cache_key] = (
        datetime.now(UTC) - snapshots.GRAPH_SNAPSHOT_CACHE_TTL - timedelta(seconds=1),
        stale,
    )

    async def load(*args: Any, **kwargs: Any) -> GraphSnapshot:
        cache[cache_key] = (datetime.now(UTC), fresh)
        return fresh

    monkeypatch.setattr(snapshots, "_load_graph_snapshot", load)

    refreshed = await snapshots._get_graph_snapshot(
        object(),
        cache_key[0],
        max_entities=cache_key[1],
        max_relationships=cache_key[2],
    )

    assert refreshed is fresh
    assert snapshots.GRAPH_SNAPSHOT_CACHE is cache
    assert cache[cache_key][1] is fresh
    assert snapshots.GRAPH_SNAPSHOT_LOADS == {}
    assert snapshots._GRAPH_SNAPSHOT_WAITERS == {}
