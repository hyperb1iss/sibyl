"""Redis event bus for cross-pod broadcasts."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from redis.asyncio import Redis

from sibyl.config import settings
from sibyl.coordination.events import EventSubscriber

log = structlog.get_logger()

PUBSUB_CHANNEL = "sibyl:websocket:events"
PUBSUB_DB = 2
# Backoff between attempts to restore a dropped subscription.
LISTEN_RETRY_INITIAL_SECONDS = 0.1
LISTEN_RETRY_MAX_SECONDS = 5.0
# Every socket operation is bounded, so a partition that silently drops
# packets surfaces as an error in seconds instead of when TCP gives up.
SOCKET_TIMEOUT_SECONDS = 5.0
SOCKET_CONNECT_TIMEOUT_SECONDS = 5.0
HEALTH_CHECK_INTERVAL_SECONDS = 15
# A subscribed connection only receives, so silence is not a symptom. The
# listener pings on this interval and treats a connection that has answered
# nothing for DEAD_AFTER_SECONDS as dead, then reconnects and resets.
PING_INTERVAL_SECONDS = 5.0
DEAD_AFTER_SECONDS = 15.0
_LIVENESS_PING_MESSAGE = "sibyl-event-bus-liveness"

type SubscribedCallback = Callable[[], Awaitable[None]]


class SubscriptionLostError(ConnectionError):
    """The subscribed connection stopped answering liveness pings."""


class RedisEventBus:
    """Redis pub/sub fan-out on one channel to this pod's subscribers.

    Redis does not queue pub/sub messages for a subscriber whose connection
    dropped, so the listener restores the subscription on its own and calls
    ``on_subscribed`` each time Redis confirms it. A consumer that cannot
    afford to miss a message (cache invalidation) uses that callback to
    discard whatever it might have missed.

    A connection that stops answering is treated like one that dropped: the
    listener pings, and when nothing comes back for ``dead_after_seconds`` it
    closes the socket, reconnects with backoff, and resubscribes.
    """

    def __init__(
        self,
        *,
        channel: str = PUBSUB_CHANNEL,
        on_subscribed: SubscribedCallback | None = None,
        ping_interval_seconds: float | None = None,
        dead_after_seconds: float | None = None,
        socket_timeout_seconds: float | None = None,
    ) -> None:
        self._channel = channel
        self._on_subscribed = on_subscribed
        self._ping_interval = ping_interval_seconds or PING_INTERVAL_SECONDS
        self._dead_after = dead_after_seconds or DEAD_AFTER_SECONDS
        self._socket_timeout = socket_timeout_seconds or SOCKET_TIMEOUT_SECONDS
        self._redis: Redis | None = None
        self._pubsub: Any | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._subscribers: list[EventSubscriber] = []
        self._subscribed = False
        self._last_error: str | None = None

    @property
    def channel(self) -> str:
        return self._channel

    @property
    def subscribed(self) -> bool:
        """Whether Redis has confirmed the subscription on a live connection."""
        return self._subscribed

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def connect(self) -> None:
        """Connect to Redis for pub/sub."""
        if self._redis is not None:
            return

        redis_host = settings.redis_host or "127.0.0.1"
        redis_port = settings.redis_port or 6381

        client = Redis(
            host=redis_host,
            port=redis_port,
            password=settings.redis_password_value or None,
            db=PUBSUB_DB,
            decode_responses=True,
            socket_timeout=self._socket_timeout,
            socket_connect_timeout=min(SOCKET_CONNECT_TIMEOUT_SECONDS, self._socket_timeout),
            socket_keepalive=True,
            health_check_interval=HEALTH_CHECK_INTERVAL_SECONDS,
        )
        try:
            await client.ping()
        except Exception:
            await client.aclose()
            raise
        self._redis = client
        log.info(
            "redis_event_bus_connected",
            host=redis_host,
            port=redis_port,
            db=PUBSUB_DB,
            channel=self._channel,
        )

    async def disconnect(self) -> None:
        """Disconnect from Redis and stop the listener."""
        if self._listener_task:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
            self._listener_task = None

        if self._pubsub:
            pubsub, self._pubsub = self._pubsub, None
            with contextlib.suppress(Exception):
                await asyncio.wait_for(pubsub.unsubscribe(self._channel), self._socket_timeout)
            with contextlib.suppress(Exception):
                await pubsub.aclose()

        if self._redis:
            client, self._redis = self._redis, None
            with contextlib.suppress(Exception):
                await client.aclose()

        self._subscribed = False
        self._subscribers.clear()
        log.info("redis_event_bus_disconnected", channel=self._channel)

    async def _require_redis(self) -> Redis:
        if self._redis is None:
            await self.connect()
        if self._redis is None:
            raise RuntimeError("Redis event bus is not connected")
        return self._redis

    async def subscribe(self, subscriber: EventSubscriber) -> None:
        """Subscribe to the Redis channel and forward messages locally."""
        redis = await self._require_redis()

        if subscriber not in self._subscribers:
            self._subscribers.append(subscriber)

        if self._pubsub is None:
            self._pubsub = redis.pubsub()
            await self._pubsub.subscribe(self._channel)
            self._listener_task = asyncio.create_task(self._listen())
            log.info("redis_event_bus_subscribed", channel=self._channel)

    async def publish(self, event: str, data: dict[str, Any], org_id: str | None = None) -> None:
        """Publish an event to the Redis channel."""
        redis = await self._require_redis()

        message = {
            "event": event,
            "data": data,
            "org_id": org_id,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        try:
            await redis.publish(self._channel, json.dumps(message))
            log.debug("redis_event_bus_published", ws_event=event, org_id=org_id)
        except Exception:
            log.exception("redis_event_bus_publish_failed", ws_event=event)
            raise

    async def _listen(self) -> None:
        """Receive events from Redis and fan them out locally, for the bus's lifetime.

        A dropped or unresponsive connection ends a pump with an error. The
        next read reconnects, and redis-py re-subscribes the channel on
        connect, so the loop only has to wait out an outage with backoff.
        """
        delay = LISTEN_RETRY_INITIAL_SECONDS
        while self._pubsub is not None:
            pubsub = self._pubsub
            try:
                await self._pump(pubsub)
                return
            except asyncio.CancelledError:
                log.debug("redis_event_bus_listener_cancelled", channel=self._channel)
                raise
            except Exception as exc:
                self._subscribed = False
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "redis_event_bus_listener_reconnecting",
                    channel=self._channel,
                    error=self._last_error,
                    retry_in_seconds=delay,
                )
                await self._drop_connection(pubsub)
            await asyncio.sleep(delay)
            delay = min(delay * 2, LISTEN_RETRY_MAX_SECONDS)

    async def _pump(self, pubsub: Any) -> None:
        """Deliver messages until the connection fails or stops answering pings."""
        last_heard = time.monotonic()
        next_ping = last_heard + self._ping_interval
        while self._pubsub is pubsub:
            message = await pubsub.get_message(timeout=min(self._ping_interval, 1.0))
            now = time.monotonic()
            if message is not None:
                last_heard = now
                await self._handle(message)
            if now - last_heard > self._dead_after:
                raise SubscriptionLostError(
                    f"no reply from Redis for {now - last_heard:.1f}s on {self._channel}"
                )
            if now >= next_ping:
                next_ping = now + self._ping_interval
                await asyncio.wait_for(pubsub.ping(_LIVENESS_PING_MESSAGE), self._socket_timeout)

    async def _drop_connection(self, pubsub: Any) -> None:
        connection = getattr(pubsub, "connection", None)
        if connection is None:
            return
        with contextlib.suppress(Exception):
            await connection.disconnect(nowait=True)

    async def _handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "subscribe":
            self._subscribed = True
            self._last_error = None
            if self._on_subscribed is not None:
                try:
                    await self._on_subscribed()
                except Exception:
                    log.exception("redis_event_bus_subscribed_callback_error")
            return
        if kind != "message":
            return

        try:
            payload = json.loads(message["data"])
            event = payload.get("event")
            data = payload.get("data", {})
            org_id = payload.get("org_id")

            if not event:
                return

            for subscriber in list(self._subscribers):
                await subscriber(event, data, org_id)
        except json.JSONDecodeError:
            log.warning("redis_event_bus_invalid_json", data=message["data"])
        except Exception:
            log.exception("redis_event_bus_callback_error")
