"""Cross-replica cache invalidation.

Some caches memoize database state per process. When a write changes that
state, the writing process updates its own caches in place; this bus carries
the news to every other process so their copies are dropped too, instead of
serving the old answer until a TTL runs out.

With the redis coordination backend, announcements travel on their own pub/sub
channel, never on the WebSocket event channel, whose events reach browsers.
With the local backend there is a single process whose caches the writer
already updated, so announcing is a no-op and nothing else changes.

Three properties keep the bus from becoming a liability on the request path:

- Announcing never waits on Redis. It queues the message and returns; a sender
  task publishes in order with bounded attempts and keeps retrying a message
  until Redis takes it, so a partition delays peers but never a logout.
- Each topic is applied by its own worker, so a slow handler (a runtime
  rebuild) cannot hold up a fast one (a session revocation).
- Pub/sub does not replay messages a subscriber missed while disconnected, so
  every (re)subscription runs the registered reset hooks: a process that might
  have missed an invalidation forgets everything it cached instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

import structlog

from sibyl.coordination import get_coordination_backend
from sibyl.coordination.events import EventBus

log = structlog.get_logger()

INVALIDATION_CHANNEL = "sibyl:cache:invalidations"
# One publish attempt; a failed attempt is retried with backoff, not dropped.
PUBLISH_ATTEMPT_TIMEOUT_SECONDS = 2.0
PUBLISH_RETRY_INITIAL_SECONDS = 0.1
PUBLISH_RETRY_MAX_SECONDS = 5.0

type InvalidationPayload = dict[str, Any]
type InvalidationHandler = Callable[[InvalidationPayload], Awaitable[None] | None]
type ResetHook = Callable[[], Awaitable[None] | None]


class InvalidationTransport(EventBus, Protocol):
    @property
    def subscribed(self) -> bool: ...


async def _settle(result: Awaitable[None] | None) -> None:
    if inspect.isawaitable(result):
        await result


@dataclass
class _TopicWorker:
    """Applies one topic's announcements in arrival order, off the listener."""

    topic: str
    handler: InvalidationHandler
    coalesce: bool
    queue: asyncio.Queue[InvalidationPayload] = field(default_factory=asyncio.Queue)
    task: asyncio.Task[None] | None = None

    def submit(self, payload: InvalidationPayload) -> None:
        if self.task is None or self.task.done():
            # A fresh queue per worker task keeps it on the running loop.
            self.queue = asyncio.Queue()
            self.task = asyncio.create_task(self._run(), name=f"invalidation:{self.topic}")
        # A coalescing handler re-reads full state, so one run that has not
        # started yet covers any number of announcements that arrive before it.
        if self.coalesce and not self.queue.empty():
            return
        self.queue.put_nowait(payload)

    async def _run(self) -> None:
        while True:
            payload = await self.queue.get()
            try:
                await _settle(self.handler(payload))
            except Exception:
                log.exception("cache_invalidation_handler_failed", topic=self.topic)
            finally:
                self.queue.task_done()

    async def drain(self) -> None:
        await self.queue.join()

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None


class CacheInvalidationBus:
    """Announces local cache invalidations and applies the ones other processes announce."""

    def __init__(self, *, origin: str | None = None) -> None:
        self.origin = origin or uuid4().hex
        self._workers: dict[str, _TopicWorker] = {}
        self._resets: dict[object, ResetHook] = {}
        self._transport: InvalidationTransport | None = None
        self._broadcasting = False
        self._outbox: deque[tuple[str, InvalidationPayload]] = deque()
        self._outbox_ready: asyncio.Event | None = None
        self._outbox_empty: asyncio.Event | None = None
        self._sender: asyncio.Task[None] | None = None
        self._publish_failures = 0
        self._last_publish_error: str | None = None

    @property
    def broadcasting(self) -> bool:
        """Whether this process announces to others (the redis backend)."""
        return self._broadcasting

    @property
    def attached(self) -> bool:
        return self._transport is not None

    @property
    def subscribed(self) -> bool:
        transport = self._transport
        return transport is not None and bool(getattr(transport, "subscribed", True))

    def status(self) -> dict[str, Any]:
        transport = self._transport
        if not self._broadcasting:
            state = "local"
        elif transport is None:
            state = "connecting"
        elif not self.subscribed:
            state = "reconnecting"
        else:
            state = "subscribed"
        return {
            "state": state,
            "pending_announcements": len(self._outbox),
            "publish_failures": self._publish_failures,
            "last_error": self._last_publish_error or getattr(transport, "last_error", None),
        }

    def register(self, topic: str, handler: InvalidationHandler, *, coalesce: bool = False) -> None:
        """Apply ``handler`` to every announcement on ``topic`` from another process."""
        self._workers[topic] = _TopicWorker(topic=topic, handler=handler, coalesce=coalesce)

    def on_reset(self, hook: ResetHook, *, name: str | None = None) -> None:
        """Run ``hook`` whenever announcements may have been missed.

        Registering again under the same name (or the same hook) replaces it.
        """
        self._resets[name if name is not None else hook] = hook

    def enable_broadcast(self) -> None:
        """Queue announcements from now on, to send once a transport is attached."""
        self._broadcasting = True

    async def attach(self, transport: InvalidationTransport) -> None:
        await transport.connect()
        try:
            await transport.subscribe(self._receive)
        except Exception:
            with contextlib.suppress(Exception):
                await transport.disconnect()
            raise
        self._transport = transport
        self._broadcasting = True
        self._ensure_sender()

    async def detach(self, *, flush_timeout: float = 2.0) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.flush(), flush_timeout)
        if self._sender is not None:
            self._sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sender
            self._sender = None
        for worker in self._workers.values():
            await worker.stop()
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.disconnect()

    async def announce(self, topic: str, payload: InvalidationPayload) -> None:
        """Queue the news for the other processes and return at once.

        The caller has already updated its own caches. Messages are sent in
        order and a failed send is retried, never dropped, so peers converge
        once Redis is reachable; the request that caused the change does not
        wait for that.
        """
        if not self._broadcasting:
            return
        self._outbox.append((topic, {"origin": self.origin, "payload": payload}))
        self._ensure_sender()
        if self._outbox_empty is not None:
            self._outbox_empty.clear()
        if self._outbox_ready is not None:
            self._outbox_ready.set()

    async def flush(self) -> None:
        """Wait until every queued announcement has been published."""
        empty = self._outbox_empty
        if not self._outbox or self._transport is None or empty is None:
            return
        await empty.wait()

    async def drain(self) -> None:
        """Wait until every received announcement has been applied."""
        for worker in list(self._workers.values()):
            await worker.drain()

    def submit_local(self, topic: str, payload: InvalidationPayload) -> None:
        """Run a topic's handler here, through its worker, as if a peer had announced."""
        worker = self._workers.get(topic)
        if worker is not None:
            worker.submit(payload)

    async def reset(self) -> None:
        for hook in list(self._resets.values()):
            try:
                await _settle(hook())
            except Exception:
                log.exception("cache_invalidation_reset_failed")

    def _ensure_sender(self) -> None:
        if self._transport is None:
            return
        if self._sender is None or self._sender.done():
            ready, empty = asyncio.Event(), asyncio.Event()
            if self._outbox:
                ready.set()
            else:
                empty.set()
            self._outbox_ready, self._outbox_empty = ready, empty
            self._sender = asyncio.create_task(self._send_outbox(ready), name="invalidation:sender")

    async def _send_outbox(self, ready: asyncio.Event) -> None:
        delay = PUBLISH_RETRY_INITIAL_SECONDS
        while True:
            if not self._outbox:
                if self._outbox_empty is not None:
                    self._outbox_empty.set()
                ready.clear()
                await ready.wait()
                continue
            transport = self._transport
            if transport is None:
                return
            topic, message = self._outbox[0]
            try:
                await asyncio.wait_for(
                    transport.publish(topic, message), PUBLISH_ATTEMPT_TIMEOUT_SECONDS
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._publish_failures += 1
                self._last_publish_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "cache_invalidation_publish_retrying",
                    topic=topic,
                    pending=len(self._outbox),
                    error=self._last_publish_error,
                    retry_in_seconds=delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, PUBLISH_RETRY_MAX_SECONDS)
                continue
            self._outbox.popleft()
            self._last_publish_error = None
            delay = PUBLISH_RETRY_INITIAL_SECONDS

    async def _receive(self, topic: str, data: dict[str, Any], org_id: str | None) -> None:
        del org_id
        if data.get("origin") == self.origin:
            return
        worker = self._workers.get(topic)
        if worker is None:
            return
        payload = data.get("payload")
        worker.submit(payload if isinstance(payload, dict) else {})


_bus = CacheInvalidationBus()


def get_cache_invalidation_bus() -> CacheInvalidationBus:
    return _bus


def build_invalidation_transport(bus: CacheInvalidationBus) -> InvalidationTransport | None:
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
    "InvalidationTransport",
    "build_invalidation_transport",
    "get_cache_invalidation_bus",
]
