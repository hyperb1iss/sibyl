"""Shared SurrealDB connection warming and health checks."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import structlog

from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient

log = structlog.get_logger()

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
        await asyncio.sleep(_health_interval_seconds())
        await asyncio.gather(
            _sweep_client("auth", _auth_client),
            _sweep_client("content", _content_client),
        )


def _health_interval_seconds() -> float:
    from sibyl.config import settings

    return settings.surreal_pool_health_interval_seconds


async def _warm_client(name: str, factory: ClientFactory) -> None:
    client = await factory()
    await client.warm_pool()
    log.info("shared_surreal_client_warmed", client=name)


async def _sweep_client(name: str, factory: ClientFactory) -> None:
    """Drop dead slots between requests so no caller pays the reconnect."""
    try:
        client = await factory()
        health = await client.ping_pool()
    except Exception as exc:
        log.warning(
            "shared_surreal_client_ping_failed",
            client=name,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return
    if health.reaped:
        log.warning(
            "shared_surreal_pool_slots_reaped",
            client=name,
            checked=health.checked,
            reaped=health.reaped,
            failures=sorted(set(health.failures)),
        )
        return
    log.debug("shared_surreal_pool_healthy", client=name, checked=health.checked)


async def _auth_client() -> DedicatedSurrealClient:
    from sibyl.persistence.surreal.auth import get_shared_surreal_auth_client

    return await get_shared_surreal_auth_client()


async def _content_client() -> DedicatedSurrealClient:
    from sibyl.persistence.surreal.content import get_shared_surreal_content_client

    return await get_shared_surreal_content_client()


__all__ = [
    "initialize_shared_surreal_connectivity",
    "start_surreal_connectivity_monitor",
    "stop_surreal_connectivity_monitor",
    "warm_shared_surreal_clients",
]
