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


def install_cache_invalidation_handlers(bus: CacheInvalidationBus) -> None:
    bus.register(
        SESSIONS_TOPIC,
        lambda payload: apply_session_invalidation(access_session_cache, payload),
    )
    bus.on_reset(access_session_cache.clear)


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
    "SESSIONS_TOPIC",
    "announce_sessions_invalidated",
    "apply_session_invalidation",
    "install_cache_invalidation_handlers",
    "start_cache_invalidation",
    "stop_cache_invalidation",
]
