"""Build progress extends readiness waits without hiding stalled work."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl_core.backends.surreal import schema_version as schema


@pytest.fixture
def clock(monkeypatch):
    time = SimpleNamespace(now=0.0)
    monkeypatch.setattr(schema, "monotonic", lambda: time.now)

    async def sleep(seconds):
        time.now += seconds

    monkeypatch.setattr(schema.asyncio, "sleep", sleep)
    return time


@pytest.mark.parametrize("counter", ["initial", "updated"])
async def test_progressing_build_outlives_original_timeout(monkeypatch, clock, counter):
    statuses = [schema.IndexBuildStatus("indexing", **{counter: i}) for i in range(4)]
    statuses.append(schema.IndexBuildStatus("ready"))
    monkeypatch.setattr(schema, "get_index_build_status", AsyncMock(side_effect=statuses))
    owner = SimpleNamespace(heartbeat=AsyncMock(), lease_seconds=60)
    ready = await schema.wait_for_index_ready(
        AsyncMock(),
        name="idx",
        table="sample",
        timeout_seconds=3,
        poll_interval_seconds=2,
        ownership=owner,
        require_status=True,
        track_progress=True,
    )
    assert ready.status == "ready"
    assert clock.now == 8
    assert owner.heartbeat.await_count == 5


async def test_growing_pending_queue_does_not_reset_stall_clock(monkeypatch, clock):
    statuses = [schema.IndexBuildStatus("indexing", initial=16, pending=i) for i in range(4)]
    status_read = AsyncMock(side_effect=statuses)
    monkeypatch.setattr(schema, "get_index_build_status", status_read)
    with pytest.raises(schema.IndexBuildStalledError):
        await schema.wait_for_index_ready(
            AsyncMock(),
            name="idx",
            table="sample",
            timeout_seconds=3,
            poll_interval_seconds=2,
            require_status=True,
            track_progress=True,
        )
    assert clock.now == 4
    assert status_read.await_count == 2


async def test_consumed_pending_work_extends_observation(monkeypatch, clock):
    statuses = [schema.IndexBuildStatus("indexing", pending=i) for i in (10, 8, 6)]
    statuses.append(schema.IndexBuildStatus("ready"))
    monkeypatch.setattr(schema, "get_index_build_status", AsyncMock(side_effect=statuses))
    ready = await schema.wait_for_index_ready(
        AsyncMock(),
        name="idx",
        table="sample",
        timeout_seconds=3,
        poll_interval_seconds=2,
        require_status=True,
        track_progress=True,
    )
    assert ready.status == "ready"
    assert clock.now == 6
