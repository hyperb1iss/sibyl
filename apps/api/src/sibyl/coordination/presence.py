"""Who is watching sibylnet channels: protocol, entry shapes and backend resolution.

Presence is derived, never maintained: every feed read notes its reader as
watching the channels it read for its wait plus ``WATCHING_TTL_SECONDS``, and
nothing else writes here. A dispatcher that peeks once a minute therefore
stays listed. Entries live in the coordination backend, in process memory for
a single process and in Redis across replicas, so every replica answers with
the same watchers; nothing is stored in the database.

A row is one (channel, agent label, session) a user read, and labels are
whatever a client sends, so each user keeps at most ``MAX_ROWS_PER_READER``
rows in an organization; a new sighting past that drops that user's oldest.
Presence then grows with members, not with how many labels anyone invents,
and a write touches only its own reader's rows.

Every write keeps the later of the stored and the new value for both a row's
expiry and its sighting, so writes from different replicas commute and a row
never expires earlier because a shorter read came in after a longer one.
Windows are measured on the backend's own clock (Redis's, across replicas),
so clock skew between replicas cannot expire a live watcher.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sibyl.coordination import CoordinationBackend, get_coordination_backend

WATCHING_TTL_SECONDS = 120
# Generous for a person running dozens of agent sessions, each reading a
# channel or two; tight enough that one member can't swell presence.
MAX_ROWS_PER_READER = 128
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
        *,
        user_id: str,
        agent: str | None,
        session: str | None,
        channels: Sequence[str | None],
        window: float,
    ) -> None:
        """Record one read of ``channels``, watching for ``window`` seconds from now."""
        ...

    async def watching(self, organization_id: str) -> list[WatcherSeen]:
        """Every watcher in the organization whose window is still open."""
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
    "MAX_ROWS_PER_READER",
    "PRESENCE_PREFIX",
    "WATCHING_TTL_SECONDS",
    "PresenceRegistry",
    "Watcher",
    "WatcherSeen",
    "get_presence",
]
