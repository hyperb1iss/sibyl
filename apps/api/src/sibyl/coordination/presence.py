"""Who is watching sibylnet channels: protocol, entry shapes and backend resolution.

Presence is derived, never maintained: every feed read notes its reader as
watching the channels it read until its wait runs out plus
``WATCHING_TTL_SECONDS``, and nothing else writes here. A dispatcher that peeks
once a minute therefore stays listed. Entries live in the coordination
backend, in process memory for a single process and in Redis across replicas,
so every replica answers with the same watchers; nothing is stored in the
database.

Every write keeps the later of the stored and the new value for both the
expiry and ``last_seen``, so writes from different replicas commute and an
entry never expires earlier because a shorter read came in after a longer
one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sibyl.coordination import CoordinationBackend, get_coordination_backend

WATCHING_TTL_SECONDS = 120
PRESENCE_PREFIX = "sibyl:net:presence:"


@dataclass(frozen=True, slots=True)
class Watcher:
    """One reader of one channel: ``channel`` is None for a read of every channel."""

    channel: str | None
    user_id: str
    agent: str | None
    session: str | None


@dataclass(frozen=True, slots=True)
class WatcherSeen:
    """A watcher still inside its window, and when it last read (epoch seconds)."""

    watcher: Watcher
    last_seen: float


class PresenceRegistry(Protocol):
    """Backend contract for derived sibylnet presence."""

    async def note(
        self,
        organization_id: str,
        watchers: Sequence[Watcher],
        *,
        now: float,
        until: float,
    ) -> None:
        """Record ``watchers`` as seen at ``now`` and watching through ``until``."""
        ...

    async def watching(self, organization_id: str, *, now: float) -> list[WatcherSeen]:
        """Every watcher in the organization whose window is still open at ``now``."""
        ...


_presence: PresenceRegistry | None = None
_presence_backend: CoordinationBackend | None = None


def get_presence() -> PresenceRegistry:
    """Return the presence registry for the active coordination backend."""
    global _presence, _presence_backend  # noqa: PLW0603

    backend = get_coordination_backend()
    if _presence is not None and _presence_backend == backend:
        return _presence

    if backend == "redis":
        from sibyl.coordination._redis.presence import RedisPresenceRegistry

        _presence = RedisPresenceRegistry()
    else:
        from sibyl.coordination._local.presence import LocalPresenceRegistry

        _presence = LocalPresenceRegistry()

    _presence_backend = backend
    return _presence


__all__ = [
    "PRESENCE_PREFIX",
    "WATCHING_TTL_SECONDS",
    "PresenceRegistry",
    "Watcher",
    "WatcherSeen",
    "get_presence",
]
