"""Redis pub/sub for cross-pod WebSocket broadcasts.

When running multiple backend pods, local WebSocket broadcasts only reach
clients connected to that specific pod. This module uses Redis pub/sub
to fan out events across all pods.

Architecture:
    Pod A (broadcast) -> Redis channel -> Pod A, Pod B, Pod C (local delivery)

Events published to Redis are received by all pods, which then broadcast
to their locally connected WebSocket clients.
"""

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from typing import Any

import structlog
from redis.asyncio import Redis

from sibyl.config import settings

log = structlog.get_logger()

# Redis pub/sub channel for WebSocket events
PUBSUB_CHANNEL = "sibyl:websocket:events"

# Use a dedicated Redis database for pub/sub (separate from graph and jobs)
PUBSUB_DB = 2


class RedisPubSub:
    """Redis pub/sub manager for cross-pod WebSocket broadcasts."""

    def __init__(self) -> None:
        self._redis: Redis | None = None
        self._pubsub: Any | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._local_broadcast_callback: Any = None

    async def connect(self) -> None:
        """Connect to Redis for pub/sub."""
        if self._redis is not None:
            return

        self._redis = Redis(
            host=settings.falkordb_host,
            port=settings.falkordb_port,
            password=settings.falkordb_password,
            db=PUBSUB_DB,
            decode_responses=True,
        )

        # Test connection
        await self._redis.ping()
        log.info(
            "redis_pubsub_connected",
            host=settings.falkordb_host,
            port=settings.falkordb_port,
            db=PUBSUB_DB,
        )

    async def disconnect(self) -> None:
        """Disconnect from Redis and stop listener."""
        if self._listener_task:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
            self._listener_task = None

        if self._pubsub:
            await self._pubsub.unsubscribe(PUBSUB_CHANNEL)
            await self._pubsub.close()
            self._pubsub = None

        if self._redis:
            await self._redis.close()
            self._redis = None

        log.info("redis_pubsub_disconnected")

    async def publish(self, event: str, data: dict[str, Any], org_id: str | None = None) -> None:
        """Publish an event to the Redis channel.

        Args:
            event: Event type name (e.g., "entity_created")
            data: Event payload
            org_id: Organization to scope broadcast to (None = all orgs)
        """
        if self._redis is None:
            await self.connect()

        message = {
            "event": event,
            "data": data,
            "org_id": org_id,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        try:
            await self._redis.publish(PUBSUB_CHANNEL, json.dumps(message))  # type: ignore[union-attr]
            log.debug("redis_pubsub_published", ws_event=event, org_id=org_id)
        except Exception:
            log.exception("redis_pubsub_publish_failed", ws_event=event)
            raise

    async def subscribe(self, local_broadcast_callback: Any) -> None:
        """Subscribe to the Redis channel and forward messages to local broadcast.

        Args:
            local_broadcast_callback: Async function(event, data, org_id) to
                broadcast to local WebSocket connections.
        """
        if self._redis is None:
            await self.connect()

        self._local_broadcast_callback = local_broadcast_callback
        self._pubsub = self._redis.pubsub()  # type: ignore[union-attr]
        await self._pubsub.subscribe(PUBSUB_CHANNEL)

        # Start background listener
        self._listener_task = asyncio.create_task(self._listen())
        log.info("redis_pubsub_subscribed", channel=PUBSUB_CHANNEL)

    async def _listen(self) -> None:
        """Background task to receive messages from Redis and broadcast locally."""
        if self._pubsub is None:
            return

        try:
            async for message in self._pubsub.listen():
                if message["type"] != "message":
                    continue

                try:
                    payload = json.loads(message["data"])
                    event = payload.get("event")
                    data = payload.get("data", {})
                    org_id = payload.get("org_id")

                    if self._local_broadcast_callback and event:
                        await self._local_broadcast_callback(event, data, org_id)

                except json.JSONDecodeError:
                    log.warning("redis_pubsub_invalid_json", data=message["data"])
                except Exception:
                    log.exception("redis_pubsub_callback_error")

        except asyncio.CancelledError:
            log.debug("redis_pubsub_listener_cancelled")
            raise
        except Exception:
            log.exception("redis_pubsub_listener_error")


# Global pub/sub instance
_pubsub: RedisPubSub | None = None


def get_pubsub() -> RedisPubSub:
    """Get or create the global pub/sub manager."""
    global _pubsub  # noqa: PLW0603
    if _pubsub is None:
        _pubsub = RedisPubSub()
    return _pubsub


async def init_pubsub(local_broadcast_callback: Any) -> None:
    """Initialize Redis pub/sub on server startup.

    Args:
        local_broadcast_callback: Async function to broadcast to local connections.
    """
    pubsub = get_pubsub()
    await pubsub.connect()
    await pubsub.subscribe(local_broadcast_callback)


async def shutdown_pubsub() -> None:
    """Shutdown Redis pub/sub on server shutdown."""
    pubsub = get_pubsub()
    await pubsub.disconnect()


async def publish_event(event: str, data: dict[str, Any], *, org_id: str | None = None) -> None:
    """Publish an event to Redis for cross-pod broadcast.

    This is the main interface for other modules to send realtime updates.
    Events are published to Redis, then each pod's listener broadcasts
    to its local WebSocket connections.

    Args:
        event: Event type name
        data: Event payload
        org_id: Organization to broadcast to (None = all clients)
    """
    pubsub = get_pubsub()
    await pubsub.publish(event, data, org_id)
