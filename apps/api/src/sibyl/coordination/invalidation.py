"""Cross-replica cache invalidation.

Some caches memoize database state per process. When a write changes that
state, the writing process updates its own caches in place; this bus carries
the news to every other process so their copies are dropped too, instead of
serving the old answer until a TTL runs out.

With the redis coordination backend, announcements travel on their own pub/sub
channel, never on the WebSocket event channel, whose events reach browsers.
With the local backend there is a single process whose caches the writer
already updated, so announcing is a no-op and nothing else changes.

Pub/sub does not replay messages a subscriber missed while disconnected, so
every (re)subscription runs the registered reset hooks: a process that might
have missed an invalidation forgets everything it cached instead.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import structlog

from sibyl.coordination import get_coordination_backend
from sibyl.coordination.events import EventBus

log = structlog.get_logger()

INVALIDATION_CHANNEL = "sibyl:cache:invalidations"

type InvalidationPayload = dict[str, Any]
type InvalidationHandler = Callable[[InvalidationPayload], Awaitable[None] | None]
type ResetHook = Callable[[], Awaitable[None] | None]


async def _settle(result: Awaitable[None] | None) -> None:
    if inspect.isawaitable(result):
        await result


class CacheInvalidationBus:
    """Announces local cache invalidations and applies the ones other processes announce."""

    def __init__(self, *, origin: str | None = None) -> None:
        self.origin = origin or uuid4().hex
        self._handlers: dict[str, InvalidationHandler] = {}
        self._resets: list[ResetHook] = []
        self._transport: EventBus | None = None

    @property
    def broadcasting(self) -> bool:
        return self._transport is not None

    def register(self, topic: str, handler: InvalidationHandler) -> None:
        """Apply ``handler`` to every announcement on ``topic`` from another process."""
        self._handlers[topic] = handler

    def on_reset(self, hook: ResetHook) -> None:
        """Run ``hook`` whenever announcements may have been missed."""
        if hook not in self._resets:
            self._resets.append(hook)

    async def attach(self, transport: EventBus) -> None:
        await transport.connect()
        await transport.subscribe(self._receive)
        self._transport = transport

    async def detach(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.disconnect()

    async def announce(self, topic: str, payload: InvalidationPayload) -> None:
        """Tell the other processes; the caller has already updated its own caches.

        A failed publish is logged rather than raised: the write it follows is
        already committed, and peers still converge when their entries expire.
        """
        transport = self._transport
        if transport is None:
            return
        try:
            await transport.publish(topic, {"origin": self.origin, "payload": payload})
        except Exception:
            log.exception("cache_invalidation_publish_failed", topic=topic)

    async def reset(self) -> None:
        for hook in list(self._resets):
            try:
                await _settle(hook())
            except Exception:
                log.exception("cache_invalidation_reset_failed")

    async def _receive(self, topic: str, data: dict[str, Any], org_id: str | None) -> None:
        del org_id
        if data.get("origin") == self.origin:
            return
        handler = self._handlers.get(topic)
        if handler is None:
            return
        payload = data.get("payload")
        await _settle(handler(payload if isinstance(payload, dict) else {}))


_bus = CacheInvalidationBus()


def get_cache_invalidation_bus() -> CacheInvalidationBus:
    return _bus


def build_invalidation_transport(bus: CacheInvalidationBus) -> EventBus | None:
    """The transport for the active coordination backend, or None for one process."""
    if get_coordination_backend() != "redis":
        return None
    from sibyl.coordination._redis.events import RedisEventBus

    return RedisEventBus(channel=INVALIDATION_CHANNEL, on_subscribed=bus.reset)


__all__ = [
    "INVALIDATION_CHANNEL",
    "CacheInvalidationBus",
    "InvalidationHandler",
    "InvalidationPayload",
    "build_invalidation_transport",
    "get_cache_invalidation_bus",
]
