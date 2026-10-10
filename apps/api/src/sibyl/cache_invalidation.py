"""Which per-process caches follow writes made on other replicas, and how.

Each topic mirrors one local action: the process that makes a change updates
its own caches as it always has, then announces the change here so every other
API replica and worker applies the same invalidation. Announcements carry
identifiers only; anything a peer needs beyond that, it re-reads from the
database.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable
from typing import Any
from uuid import UUID

import structlog

from sibyl.auth.session_cache import AccessSessionCache, access_session_cache
from sibyl.coordination.invalidation import (
    CacheInvalidationBus,
    InvalidationPayload,
    build_invalidation_transport,
    get_cache_invalidation_bus,
)

log = structlog.get_logger()

SESSIONS_TOPIC = "auth.sessions.invalidated"
SETTINGS_TOPIC = "settings.changed"
RUNTIME_SETTINGS_TOPIC = "settings.runtime.changed"
LLM_RUNTIME_TOPIC = "llm.runtime.invalidated"
# Backoff between attempts to join the channel while Redis is unreachable.
ATTACH_RETRY_INITIAL_SECONDS = 0.5
ATTACH_RETRY_MAX_SECONDS = 30.0

_attach_task: asyncio.Task[None] | None = None


def _uuid_strings(values: Iterable[UUID | str | None]) -> list[str]:
    return sorted({str(value) for value in values if value is not None})


def _uuids(payload: InvalidationPayload, key: str) -> list[UUID]:
    raw = payload.get(key)
    if not isinstance(raw, list):
        return []
    parsed: list[UUID] = []
    for value in raw:
        try:
            parsed.append(UUID(str(value)))
        except ValueError:
            log.warning("cache_invalidation_bad_uuid", key=key)
    return parsed


async def announce_sessions_invalidated(
    *,
    session_ids: Iterable[UUID | str | None] = (),
    user_ids: Iterable[UUID | str | None] = (),
    organization_ids: Iterable[UUID | str | None] = (),
) -> None:
    """Drop these sessions from every other replica's validity cache.

    Callers revoke or delete the sessions in the database and update the local
    cache first, so a peer that re-reads after dropping its entry sees the
    revocation.
    """
    payload = {
        "session_ids": _uuid_strings(session_ids),
        "user_ids": _uuid_strings(user_ids),
        "organization_ids": _uuid_strings(organization_ids),
    }
    if not any(payload.values()):
        return
    await get_cache_invalidation_bus().announce(SESSIONS_TOPIC, payload)


def apply_session_invalidation(cache: AccessSessionCache, payload: InvalidationPayload) -> None:
    for session_id in _uuids(payload, "session_ids"):
        cache.invalidate(session_id)
    for user_id in _uuids(payload, "user_ids"):
        cache.invalidate_user(user_id)
    for organization_id in _uuids(payload, "organization_ids"):
        cache.invalidate_organization(organization_id)


def _strings(payload: InvalidationPayload, key: str) -> list[str]:
    raw = payload.get(key)
    if not isinstance(raw, list):
        return []
    return [str(value) for value in raw if isinstance(value, str) and value]


async def announce_settings_changed(keys: Iterable[str]) -> None:
    """Drop these system settings from every other process's settings cache."""
    payload = {"keys": sorted(set(keys))}
    if payload["keys"]:
        await get_cache_invalidation_bus().announce(SETTINGS_TOPIC, payload)


async def announce_runtime_settings_changed(keys: Iterable[str]) -> None:
    """Have every other process re-apply these settings to its runtime, as this one did."""
    payload = {"keys": sorted(set(keys))}
    if payload["keys"]:
        await get_cache_invalidation_bus().announce(RUNTIME_SETTINGS_TOPIC, payload)


async def announce_llm_runtime_invalidated(surface: str | None) -> None:
    """Drop resolved LLM config for one surface, or all of them, everywhere else."""
    await get_cache_invalidation_bus().announce(LLM_RUNTIME_TOPIC, {"surface": surface})


def _forget_settings(payload: InvalidationPayload) -> None:
    from sibyl.services.settings import get_settings_service

    get_settings_service().forget(_strings(payload, "keys"))


async def _sync_runtime_settings(payload: InvalidationPayload) -> None:
    from sibyl.services.settings import sync_runtime_settings

    # The whole stored state is re-read, so the keys named only label the log.
    keys = ", ".join(_strings(payload, "keys")) or "all"
    await sync_runtime_settings(reason=f"announced by another process: {keys}")


async def _invalidate_llm_runtime(payload: InvalidationPayload) -> None:
    from sibyl.ai.llm.service import invalidate_local_llm_runtime
    from sibyl_core.ai.llm.config import LLMSurface

    raw = payload.get("surface")
    try:
        surface = LLMSurface(raw) if isinstance(raw, str) else None
    except ValueError:
        # A surface this build does not know; forgetting all of them is safe.
        surface = None
    await invalidate_local_llm_runtime(surface)


def _clear_settings() -> None:
    from sibyl.services.settings import get_settings_service

    get_settings_service().clear_cache()


async def _clear_llm_runtime() -> None:
    from sibyl.ai.llm.service import invalidate_local_llm_runtime

    await invalidate_local_llm_runtime(None)


def install_cache_invalidation_handlers(bus: CacheInvalidationBus) -> None:
    bus.register(
        SESSIONS_TOPIC,
        lambda payload: apply_session_invalidation(access_session_cache, payload),
    )
    bus.register(SETTINGS_TOPIC, _forget_settings)
    # A rebuild re-reads every stored runtime setting, so queued runs collapse.
    bus.register(RUNTIME_SETTINGS_TOPIC, _sync_runtime_settings, coalesce=True)
    bus.register(LLM_RUNTIME_TOPIC, _invalidate_llm_runtime)
    bus.on_reset(access_session_cache.clear)
    bus.on_reset(_clear_settings)
    bus.on_reset(_clear_llm_runtime)
    # A runtime-settings change may have been missed too. Re-syncing applies
    # the same rule as startup, on the topic's own worker so the rebuild does
    # not hold up the session invalidations that follow.
    bus.on_reset(
        lambda: bus.submit_local(RUNTIME_SETTINGS_TOPIC, {"keys": ["resubscribed"]}),
        name=RUNTIME_SETTINGS_TOPIC,
    )


async def _attach_until_joined(bus: CacheInvalidationBus) -> None:
    """Join the channel, retrying with backoff for as long as Redis is unreachable."""
    delay = ATTACH_RETRY_INITIAL_SECONDS
    attempt = 0
    while True:
        attempt += 1
        transport = build_invalidation_transport(bus)
        if transport is None:
            return
        try:
            await bus.attach(transport)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "cache_invalidation_attach_retrying",
                attempt=attempt,
                error=f"{type(exc).__name__}: {exc}",
                retry_in_seconds=delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, ATTACH_RETRY_MAX_SECONDS)
            continue
        log.info("cache_invalidation_joined", attempts=attempt)
        return


async def start_cache_invalidation() -> bool:
    """Join the invalidation channel; False when this process is the only one.

    Joining never blocks startup. With Redis unreachable the process keeps
    retrying in the background; announcements made meanwhile are queued, and
    the confirmed subscription runs the reset hooks, which clear the caches
    and re-sync runtime settings, so the process catches up on whatever it
    missed. ``cache_invalidation_status`` reports progress.
    """
    global _attach_task  # noqa: PLW0603
    bus = get_cache_invalidation_bus()
    install_cache_invalidation_handlers(bus)
    if build_invalidation_transport(bus) is None:
        return False
    bus.enable_broadcast()
    if _attach_task is None or _attach_task.done():
        _attach_task = asyncio.create_task(
            _attach_until_joined(bus), name="cache-invalidation-attach"
        )
    return True


async def stop_cache_invalidation() -> None:
    global _attach_task
    task, _attach_task = _attach_task, None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await get_cache_invalidation_bus().detach()


def cache_invalidation_status() -> dict[str, Any]:
    """This process's view of the invalidation channel, for health payloads."""
    return get_cache_invalidation_bus().status()


__all__ = [
    "LLM_RUNTIME_TOPIC",
    "RUNTIME_SETTINGS_TOPIC",
    "SESSIONS_TOPIC",
    "SETTINGS_TOPIC",
    "announce_llm_runtime_invalidated",
    "announce_runtime_settings_changed",
    "announce_sessions_invalidated",
    "announce_settings_changed",
    "apply_session_invalidation",
    "cache_invalidation_status",
    "install_cache_invalidation_handlers",
    "start_cache_invalidation",
    "stop_cache_invalidation",
]
