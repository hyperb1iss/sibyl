"""Redis broker health, which readiness, the jobs API and dev status report.

Under Redis coordination the API's readiness probe asks the broker whether the
queue is reachable, so a broken health read keeps every replica out of
service. The stand-in pool below has the surface of ``ArqRedis`` (a
``redis.asyncio`` client) and nothing more; the same checks run against a real
Valkey and arq worker when SIBYL_LIVE_REDIS_HOST and SIBYL_LIVE_REDIS_PORT are
set.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from unittest.mock import patch

import pytest

from sibyl.api.readiness import check_coordination_ready
from sibyl.config import settings
from sibyl.coordination import broker as broker_module
from sibyl.coordination._redis.broker import RedisQueueBroker

QUEUE = "arq:queue"
WORKER_HEARTBEAT = "arq:queue:health-check"


class ArqRedisSurface:
    """Answers the calls a redis.asyncio client answers, for one queue."""

    def __init__(self, *, queued: int = 0, heartbeat: bytes | None = None) -> None:
        self.queued = queued
        self.heartbeat = heartbeat

    async def info(self) -> dict[str, object]:
        return {"redis_version": "8.0.0", "connected_clients": 3, "used_memory_human": "1M"}

    async def zcard(self, key: str) -> int:
        assert key == QUEUE
        return self.queued

    async def get(self, key: str) -> bytes | None:
        assert key == WORKER_HEARTBEAT
        return self.heartbeat


def _broker_on(pool: ArqRedisSurface) -> RedisQueueBroker:
    broker = RedisQueueBroker()
    broker._pool = pool  # type: ignore[assignment]
    return broker


async def test_reachable_redis_is_healthy_with_its_queue_depth() -> None:
    health = await _broker_on(ArqRedisSurface(queued=3)).health()

    assert health["status"] == "healthy"
    assert health["queue_healthy"] is True
    assert health["queue_depth"] == 3
    assert health["worker_healthy"] is False


async def test_a_live_worker_heartbeat_marks_the_worker_healthy() -> None:
    health = await _broker_on(ArqRedisSurface(heartbeat=b"j_complete=0")).health()

    assert health["worker_healthy"] is True


async def test_readiness_accepts_reachable_redis_coordination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(settings.__dict__, "coordination_backend", "redis")
    broker = _broker_on(ArqRedisSurface())

    with patch("sibyl.coordination.broker.get_broker", lambda: broker):
        status = await check_coordination_ready()

    assert status.ready is True, status.detail
    assert status.backend == "redis"


def _live_redis() -> tuple[str, int]:
    host = os.environ.get("SIBYL_LIVE_REDIS_HOST", "")
    port = os.environ.get("SIBYL_LIVE_REDIS_PORT", "")
    if not host or not port:
        pytest.skip("Redis-backed broker health tests need SIBYL_LIVE_REDIS_HOST/PORT")
    return host, int(port)


async def test_live_valkey_is_ready_and_sees_a_running_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arq import Worker, func

    host, port = _live_redis()
    for field, value in {
        "coordination_backend": "redis",
        "redis_host": host,
        "redis_port": port,
        "redis_jobs_db": 12,
    }.items():
        monkeypatch.setitem(settings.__dict__, field, value)
    monkeypatch.setattr(broker_module, "_broker", None)
    monkeypatch.setattr(broker_module, "_broker_backend", None)
    broker = broker_module.get_broker()
    pool = await broker.get_pool()
    await pool.delete(WORKER_HEARTBEAT)

    async def noop(_ctx) -> None:
        return None

    worker = Worker(
        functions=[func(noop, name="noop")],
        redis_settings=broker.get_redis_settings(),
        handle_signals=False,
        poll_delay=0.05,
    )
    worker_task: asyncio.Task[None] | None = None
    try:
        idle = await broker.health()
        assert idle["status"] == "healthy"
        assert idle["queue_healthy"] is True
        assert idle["worker_healthy"] is False
        assert (await check_coordination_ready()).ready is True

        worker_task = asyncio.create_task(worker.async_run())
        for _ in range(200):
            if await pool.get(WORKER_HEARTBEAT) is not None:
                break
            await asyncio.sleep(0.05)

        assert (await broker.health())["worker_healthy"] is True
    finally:
        await worker.close()
        if worker_task is not None:
            worker_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await worker_task
        await pool.delete(WORKER_HEARTBEAT)
        await broker.close_pool()


async def test_live_worker_that_dies_stops_counting_within_its_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker killed mid-run leaves its heartbeat behind; it must not linger."""
    from arq import Worker, func

    from sibyl.jobs.worker import WorkerSettings

    host, port = _live_redis()
    for field, value in {
        "coordination_backend": "redis",
        "redis_host": host,
        "redis_port": port,
        "redis_jobs_db": 12,
    }.items():
        monkeypatch.setitem(settings.__dict__, field, value)
    monkeypatch.setattr(broker_module, "_broker", None)
    monkeypatch.setattr(broker_module, "_broker_backend", None)
    broker = broker_module.get_broker()
    pool = await broker.get_pool()
    await pool.delete(WORKER_HEARTBEAT)
    interval = WorkerSettings.health_check_interval

    async def noop(_ctx) -> None:
        return None

    worker = Worker(
        functions=[func(noop, name="noop")],
        redis_settings=broker.get_redis_settings(),
        handle_signals=False,
        poll_delay=0.05,
        health_check_interval=interval,
    )
    worker_task = asyncio.create_task(worker.async_run())
    try:
        for _ in range(200):
            if await pool.get(WORKER_HEARTBEAT) is not None:
                break
            await asyncio.sleep(0.05)
        assert (await broker.health())["worker_healthy"] is True

        # Killed, not shut down: nothing deletes the heartbeat on the way out.
        worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await worker_task
        assert 0 < await pool.pttl(WORKER_HEARTBEAT) <= (interval + 1) * 1000

        for _ in range((interval + 3) * 4):
            if not (await broker.health())["worker_healthy"]:
                break
            await asyncio.sleep(0.25)
        assert (await broker.health())["worker_healthy"] is False
    finally:
        await worker.close()
        await pool.delete(WORKER_HEARTBEAT)
        await broker.close_pool()


def test_workers_refresh_their_heartbeat_every_few_seconds() -> None:
    from sibyl.jobs.worker import WORKER_HEARTBEAT_SECONDS, WorkerSettings

    assert WorkerSettings.health_check_interval == WORKER_HEARTBEAT_SECONDS
    assert WORKER_HEARTBEAT_SECONDS <= 15
