"""Local in-process presence registry."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sibyl.coordination.presence import Watcher, WatcherSeen


@dataclass(slots=True)
class _Window:
    last_seen: float
    until: float


class LocalPresenceRegistry:
    """Keep watchers in memory, by organization.

    Expired entries are dropped whenever their organization is written or
    read, so the registry holds at most the watchers of the last few minutes.
    """

    def __init__(self) -> None:
        self._orgs: dict[str, dict[Watcher, _Window]] = {}

    async def note(
        self,
        organization_id: str,
        watchers: Sequence[Watcher],
        *,
        now: float,
        until: float,
    ) -> None:
        """Record watchers, keeping the later expiry and sighting of any stored one."""
        if not watchers:
            return
        entries = self._live(organization_id, now)
        if entries is None:
            entries = self._orgs[organization_id] = {}
        for watcher in watchers:
            window = entries.get(watcher)
            if window is None:
                entries[watcher] = _Window(last_seen=now, until=until)
                continue
            window.last_seen = max(window.last_seen, now)
            window.until = max(window.until, until)

    async def watching(self, organization_id: str, *, now: float) -> list[WatcherSeen]:
        """Watchers whose window is still open at ``now``."""
        entries = self._live(organization_id, now)
        if not entries:
            return []
        return [
            WatcherSeen(watcher=watcher, last_seen=window.last_seen)
            for watcher, window in entries.items()
        ]

    def _live(self, organization_id: str, now: float) -> dict[Watcher, _Window] | None:
        entries = self._orgs.get(organization_id)
        if entries is None:
            return None
        for watcher in [watcher for watcher, window in entries.items() if window.until <= now]:
            del entries[watcher]
        if not entries:
            del self._orgs[organization_id]
            return None
        return entries
