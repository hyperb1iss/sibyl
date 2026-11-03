"""Wake long-polling sibylnet readers when a matching message is posted.

A feed reader that found nothing used to re-query the database about once a
second for as long as it waited, so idle watchers cost one query each per
second whether or not anyone posted. Now a post publishes
``net_message_posted`` on the coordination event bus (in-process locally,
Redis pub/sub across pods), every pod's subscriber hands it to this registry,
and only the waiters whose organization, channels and thread match are woken.

A woken waiter re-reads once the new message is out of the feed's settle
window. The wake time is measured from when the event arrived here, which is
after the write committed, so by then the database clock has passed the
message's ``created_at`` plus the window. The bus is fire-and-forget (a
Redis reconnect drops whatever was published meanwhile), so a waiter also
re-reads on a slow safety interval; that interval, not a poll, bounds how
late a message can be after a lost event.

A waiter registers before its first read. A post that lands between the read
and the wait still wakes it, and a post announced shortly before it
registered, still inside the settle window, is remembered for that window so
the new waiter is woken for it too.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import structlog

from sibyl.api.event_types import WSEvent
from sibyl_core.services.net import SETTLE_SECONDS

log = structlog.get_logger()

# How long after an event a waiter re-reads: the settle window plus a margin
# for the time between the database stamping the row and the event arriving.
WAKE_AFTER_SECONDS = SETTLE_SECONDS + 0.25
# A waiter re-reads at least this often even with no event, which is the most
# a message can be delayed if the bus drops its announcement.
SAFETY_RECHECK_SECONDS = 5.0


@dataclass(eq=False, slots=True)
class NetWaiter:
    """One long-poll read: what it watches, and when it should read again."""

    organization_id: str
    channels: frozenset[str] | None
    thread: str | None
    due: float | None = None
    signal: asyncio.Event = field(default_factory=asyncio.Event)

    def matches(self, channel: str, thread: str) -> bool:
        if self.channels is not None and channel not in self.channels:
            return False
        return self.thread is None or self.thread == thread

    def wake_at(self, when: float) -> None:
        # The earliest pending wake wins: one re-read picks up every message
        # that settled by then, and anything later wakes it again.
        if self.due is None or when < self.due:
            self.due = when
        self.signal.set()

    async def wait(self, at_most: float) -> None:
        """Return when a matching message has settled, or after ``at_most`` seconds."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + at_most
        try:
            await asyncio.wait_for(self.signal.wait(), at_most)
        except TimeoutError:
            return
        due = self.due if self.due is not None else loop.time()
        self.signal.clear()
        self.due = None
        delay = min(due, deadline) - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)


@dataclass(slots=True)
class _Recent:
    channel: str
    thread: str
    wake_at: float


class NetWakeups:
    """This process's long-poll waiters, by organization."""

    def __init__(self) -> None:
        self._waiters: dict[str, set[NetWaiter]] = {}
        self._recent: dict[str, deque[_Recent]] = {}

    @contextmanager
    def watch(
        self, organization_id: str, channels: frozenset[str] | None, thread: str | None
    ) -> Generator[NetWaiter]:
        """Register a waiter for the duration of one long-poll read."""
        waiter = NetWaiter(organization_id=organization_id, channels=channels, thread=thread)
        now = asyncio.get_running_loop().time()
        for recent in self._live_recent(organization_id, now):
            if waiter.matches(recent.channel, recent.thread):
                waiter.wake_at(recent.wake_at)
        self._waiters.setdefault(organization_id, set()).add(waiter)
        try:
            yield waiter
        finally:
            waiters = self._waiters.get(organization_id)
            if waiters is not None:
                waiters.discard(waiter)
                if not waiters:
                    del self._waiters[organization_id]

    def notify(self, organization_id: str, channel: str, thread: str) -> int:
        """Wake the matching waiters; returns how many were woken."""
        now = asyncio.get_running_loop().time()
        wake_at = now + WAKE_AFTER_SECONDS
        recent = self._recent.setdefault(organization_id, deque())
        recent.append(_Recent(channel=channel, thread=thread, wake_at=wake_at))
        self._live_recent(organization_id, now)
        woken = 0
        for waiter in self._waiters.get(organization_id, ()):
            if waiter.matches(channel, thread):
                waiter.wake_at(wake_at)
                woken += 1
        return woken

    def waiting(self, organization_id: str | None = None) -> int:
        if organization_id is not None:
            return len(self._waiters.get(organization_id, ()))
        return sum(len(waiters) for waiters in self._waiters.values())

    def _live_recent(self, organization_id: str, now: float) -> deque[_Recent]:
        recent = self._recent.get(organization_id)
        if recent is None:
            return deque()
        while recent and recent[0].wake_at <= now:
            recent.popleft()
        if not recent:
            del self._recent[organization_id]
        return recent


net_wakeups = NetWakeups()


async def net_wakeup_subscriber(event: str, data: dict[str, Any], org_id: str | None) -> None:
    """Event-bus subscriber: hand a posted message to this process's waiters."""
    if event != WSEvent.NET_MESSAGE_POSTED or org_id is None:
        return
    channel = data.get("channel")
    thread = data.get("thread")
    if not isinstance(channel, str) or not isinstance(thread, str):
        return
    net_wakeups.notify(org_id, channel, thread)


async def announce_net_message(
    *, organization_id: str, channel: str, thread: str, message_id: str
) -> None:
    """Publish a stored message on the event bus; never fails the post that stored it."""
    from sibyl.api.pubsub import publish_event

    try:
        await publish_event(
            WSEvent.NET_MESSAGE_POSTED,
            {
                "organization_id": organization_id,
                "channel": channel,
                "thread": thread,
                "message_id": message_id,
            },
            org_id=organization_id,
        )
    except Exception as exc:
        # Waiters still find the message on their safety recheck.
        log.warning("net_message_announce_failed", error=str(exc), organization_id=organization_id)


__all__ = [
    "SAFETY_RECHECK_SECONDS",
    "WAKE_AFTER_SECONDS",
    "NetWaiter",
    "NetWakeups",
    "announce_net_message",
    "net_wakeup_subscriber",
    "net_wakeups",
]
