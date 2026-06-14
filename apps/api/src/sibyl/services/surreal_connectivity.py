"""Shared SurrealDB connection warming and health checks."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import structlog

from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient

log = structlog.get_logger()

_CHECK_INTERVAL_SECONDS = 30.0
_monitor_task: asyncio.Task[None] | None = None

type ClientFactory = Callable[[], Awaitable[DedicatedSurrealClient]]


async def initialize_shared_surreal_connectivity() -> None:
    try:
        await warm_shared_surreal_clients()
    except Exception as exc:
        log.warning("shared_surreal_client_warm_failed", error=str(exc))
    start_surreal_connectivity_monitor()


async def warm_shared_surreal_clients() -> None:
    await asyncio.gather(
        _warm_client("auth", _auth_client),
        _warm_client("content", _content_client),
        _warm_client("core_content", _core_content_client),
    )


def start_surreal_connectivity_monitor() -> None:
    global _monitor_task  # noqa: PLW0603
    if _monitor_task is not None and not _monitor_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _monitor_task = loop.create_task(_monitor_loop())


async def stop_surreal_connectivity_monitor() -> None:
    global _monitor_task  # noqa: PLW0603
    task = _monitor_task
    _monitor_task = None
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _monitor_loop() -> None:
    while True:
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
        await asyncio.gather(
            _ping_client("auth", _auth_client),
            _ping_client("content", _content_client),
            _ping_client("core_content", _core_content_client),
        )


async def _warm_client(name: str, factory: ClientFactory) -> None:
    client = await factory()
    await client.warm_pool()
    log.info("shared_surreal_client_warmed", client=name)


async def _ping_client(name: str, factory: ClientFactory) -> None:
    try:
        client = await factory()
        await client.ping()
    except Exception as exc:
        log.warning("shared_surreal_client_ping_failed", client=name, error=str(exc))


async def _auth_client() -> DedicatedSurrealClient:
    from sibyl.persistence.surreal.auth import get_shared_surreal_auth_client

    return await get_shared_surreal_auth_client()


async def _content_client() -> DedicatedSurrealClient:
    from sibyl.persistence.surreal.content import get_shared_surreal_content_client

    return await get_shared_surreal_content_client()


async def _core_content_client() -> DedicatedSurrealClient:
    from sibyl_core.services.surreal_content import get_shared_surreal_content_client

    return await get_shared_surreal_content_client()


__all__ = [
    "initialize_shared_surreal_connectivity",
    "start_surreal_connectivity_monitor",
    "stop_surreal_connectivity_monitor",
    "warm_shared_surreal_clients",
]
