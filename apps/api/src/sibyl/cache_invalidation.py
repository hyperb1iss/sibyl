"""Which per-process caches follow writes made on other replicas, and how.

Each topic mirrors one local action: the process that makes a change updates
its own caches as it always has, then announces the change here so every other
API replica and worker applies the same invalidation. Announcements carry
identifiers only; anything a peer needs beyond that, it re-reads from the
database.
"""

from __future__ import annotations

from collections.abc import Iterable
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


async def _apply_runtime_settings(payload: InvalidationPayload) -> None:
    from sibyl.services.settings import apply_runtime_settings_change

    await apply_runtime_settings_change(_strings(payload, "keys"))


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
    bus.register(RUNTIME_SETTINGS_TOPIC, _apply_runtime_settings)
    bus.register(LLM_RUNTIME_TOPIC, _invalidate_llm_runtime)
    # Caches only: a missed runtime-settings message is not replayed here,
    # because re-reading every setting into the environment would let stored
    # values override what the deployment set, which startup never does.
    bus.on_reset(access_session_cache.clear)
    bus.on_reset(_clear_settings)
    bus.on_reset(_clear_llm_runtime)


async def start_cache_invalidation() -> bool:
    """Join the invalidation channel; False when this process is the only one."""
    bus = get_cache_invalidation_bus()
    install_cache_invalidation_handlers(bus)
    transport = build_invalidation_transport(bus)
    if transport is None:
        return False
    await bus.attach(transport)
    return True


async def stop_cache_invalidation() -> None:
    await get_cache_invalidation_bus().detach()


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
    "install_cache_invalidation_handlers",
    "start_cache_invalidation",
    "stop_cache_invalidation",
]
