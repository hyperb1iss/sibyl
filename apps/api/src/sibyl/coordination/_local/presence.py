"""Local in-process presence registry."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sibyl.coordination.presence import MAX_ROWS_PER_READER, Watcher, WatcherSeen

type _Row = tuple[str | None, str | None, str | None]
"""Channel, agent label, session."""


@dataclass(slots=True)
class _Window:
    last_seen: float
    until: float


class LocalPresenceRegistry:
    """Keep watchers in memory, by organization and then by reader.

    A write touches only its reader's rows: it drops their expired rows,
    records the new ones, and drops their oldest sightings past
    ``MAX_ROWS_PER_READER``. A read drops every expired row it passes.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._orgs: dict[str, dict[str, dict[_Row, _Window]]] = {}

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
        """Record one read, keeping the later expiry and sighting of any stored row."""
        if not channels:
            return
        now = self._clock()
        until = now + window
        readers = self._orgs.setdefault(organization_id, {})
        rows = readers.setdefault(user_id, {})
        for row in [row for row, held in rows.items() if held.until <= now]:
            del rows[row]
        for channel in channels:
            held = rows.get((channel, agent, session))
            if held is None:
                rows[(channel, agent, session)] = _Window(last_seen=now, until=until)
                continue
            held.last_seen = max(held.last_seen, now)
            held.until = max(held.until, until)
        excess = len(rows) - MAX_ROWS_PER_READER
        if excess > 0:
            oldest = sorted(rows, key=lambda row: rows[row].last_seen)[:excess]
            for row in oldest:
                del rows[row]

    async def watching(self, organization_id: str) -> list[WatcherSeen]:
        """Watchers whose window is still open."""
        readers = self._orgs.get(organization_id)
        if not readers:
            return []
        now = self._clock()
        found: list[WatcherSeen] = []
        for user_id in list(readers):
            rows = readers[user_id]
            for row in [row for row, held in rows.items() if held.until <= now]:
                del rows[row]
            if not rows:
                del readers[user_id]
                continue
            found.extend(
                WatcherSeen(
                    watcher=Watcher(channel=channel, user_id=user_id, agent=agent, session=session),
                    last_seen=held.last_seen,
                )
                for (channel, agent, session), held in rows.items()
            )
        if not readers:
            del self._orgs[organization_id]
        return found
