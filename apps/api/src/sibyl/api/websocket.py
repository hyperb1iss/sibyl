"""WebSocket connection manager for realtime updates.

Broadcasts entity changes, search results, and system events to connected clients.
Scoped by organization for multi-tenant security.

Multi-pod Architecture:
    When running multiple backend pods, broadcasts use Redis pub/sub to fan out
    events across all pods. Each pod subscribes to the Redis channel and forwards
    received messages to its locally connected WebSocket clients.

    Pod A (event) -> Redis pub/sub -> Pod A, B, C (local broadcast to clients)

For single-pod deployments, broadcasts work locally without Redis.
"""

import asyncio
import contextlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from starlette.websockets import WebSocket, WebSocketDisconnect

from sibyl import config as config_module
from sibyl.api.event_types import WSEvent
from sibyl.auth.websocket import resolve_active_websocket_claims
from sibyl.persistence.read_memo import invalidate_org_read_memos
from sibyl_core.observability import telemetry_registry

log = structlog.get_logger()

# Flag to track if Redis pub/sub is available
_pubsub_enabled: bool = False

# Events that mean the organization's graph rows changed. The per-org read
# memos (task rollups, type counts) are dropped on these before the fan-out,
# so the refetch every tab issues in response computes against fresh rows.
READ_MEMO_INVALIDATING_EVENTS = frozenset(
    {
        WSEvent.ENTITY_CREATED,
        WSEvent.ENTITY_UPDATED,
        WSEvent.ENTITY_DELETED,
        WSEvent.ENTITY_PENDING,
        WSEvent.GRAPH_UPDATED,
        WSEvent.NOTE_CREATED,
        WSEvent.NOTE_PENDING,
    }
)

# Messages a connection may have waiting for its reader before it is dropped.
OUTBOX_LIMIT = 256


@dataclass
class Connection:
    """WebSocket connection with org context."""

    websocket: WebSocket
    org_id: str | None = None
    last_activity: datetime | None = None
    last_heartbeat_sent_at: datetime | None = None
    pending_pong: bool = False
    topics: frozenset[str] | None = None
    # Outbound messages wait here for this connection's own sender task, so
    # a reader that has stopped draining its socket holds up nothing but
    # itself: not the request that broadcast, not the heartbeat, and not
    # the other connections.
    outbox: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=OUTBOX_LIMIT)
    )
    sender: asyncio.Task[None] | None = None


class ConnectionManager:
    """Manages WebSocket connections and broadcasts events by organization.

    Sends never run on the caller's task. Each connection has a bounded
    outbox and a sender task; a broadcast only queues, and a client whose
    outbox overflows, whose send times out or fails, is dropped and closed.
    """

    # Heartbeat interval in seconds
    HEARTBEAT_INTERVAL = 30
    # How long to wait for pong before considering connection dead
    PONG_TIMEOUT = 10
    # Longest one send may take before the connection counts as gone.
    SEND_TIMEOUT = 5.0

    def __init__(self) -> None:
        self.active_connections: list[Connection] = []
        self._lock = asyncio.Lock()
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def connect(self, websocket: WebSocket, org_id: str | None = None) -> None:
        """Accept and register a new WebSocket connection with org context."""
        await websocket.accept()
        conn = Connection(websocket=websocket, org_id=org_id, last_activity=datetime.now(UTC))
        async with self._lock:
            self.active_connections.append(conn)
            self._start_sender(conn)
            # Start heartbeat task if not running
            if self._heartbeat_task is None or self._heartbeat_task.done():
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            active = len(self.active_connections)
        telemetry_registry().record_websocket_connections(active=active)
        log.info(
            "websocket_connected",
            total_connections=active,
            org_id=org_id,
        )

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection and stop its sender."""
        async with self._lock:
            removed = [c for c in self.active_connections if c.websocket == websocket]
            self.active_connections = [
                c for c in self.active_connections if c.websocket != websocket
            ]
            active = len(self.active_connections)
        current = asyncio.current_task()
        for conn in removed:
            if conn.sender is not None and conn.sender is not current:
                conn.sender.cancel()
        telemetry_registry().record_websocket_connections(active=active)
        log.info("websocket_disconnected", total_connections=active)

    def _start_sender(self, conn: Connection) -> None:
        if conn.sender is None or conn.sender.done():
            conn.sender = asyncio.create_task(self._send_loop(conn))

    def _enqueue(self, conn: Connection, message: dict[str, Any]) -> bool:
        """Hand a message to the connection's sender; False when its outbox is full."""
        try:
            conn.outbox.put_nowait(message)
        except asyncio.QueueFull:
            return False
        self._start_sender(conn)
        return True

    async def _send_loop(self, conn: Connection) -> None:
        """Deliver one connection's outbox in order, each send bounded by ``SEND_TIMEOUT``."""
        while True:
            message = await conn.outbox.get()
            try:
                async with asyncio.timeout(self.SEND_TIMEOUT):
                    await conn.websocket.send_json(message)
            except asyncio.CancelledError:
                conn.outbox.task_done()
                raise
            except Exception as exc:
                conn.outbox.task_done()
                await self._drop(conn, reason=type(exc).__name__)
                return
            conn.outbox.task_done()

    async def _drop(self, conn: Connection, *, reason: str) -> None:
        """Forget a connection that cannot keep up and close its socket."""
        log.info("websocket_dropped", org_id=conn.org_id, reason=reason, queued=conn.outbox.qsize())
        await self.disconnect(conn.websocket)
        with contextlib.suppress(Exception):
            async with asyncio.timeout(self.SEND_TIMEOUT):
                await conn.websocket.close(code=1011)

    async def flush(self) -> None:
        """Wait until every active connection's outbox has been sent."""
        async with self._lock:
            connections = list(self.active_connections)
        await asyncio.gather(*(conn.outbox.join() for conn in connections))

    async def broadcast(self, event: str, data: dict[str, Any], org_id: str | None = None) -> None:
        """Queue an event for the clients in the same organization and return.

        Args:
            event: Event type name.
            data: Event payload.
            org_id: If provided, only broadcast to clients in this org.
                   If None, broadcast to all clients (system events).
        """
        if org_id and event in READ_MEMO_INVALIDATING_EVENTS:
            invalidate_org_read_memos(org_id)

        if not self.active_connections:
            return

        message = {
            "event": event,
            "data": data,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        # Filter connections by org
        async with self._lock:
            if org_id:
                connections = [
                    c
                    for c in self.active_connections
                    if c.org_id == org_id and _connection_accepts_event(c, event)
                ]
            else:
                connections = [
                    c for c in self.active_connections if _connection_accepts_event(c, event)
                ]

        # A full outbox means the client has stopped reading; it is dropped
        # rather than allowed to pile up messages or stall anyone else.
        dropped = [conn for conn in connections if not self._enqueue(conn, message)]
        if dropped:
            await asyncio.gather(*(self._drop(conn, reason="outbox_full") for conn in dropped))

        if connections:
            telemetry_registry().record_websocket_broadcast(
                event=event,
                recipients=len(connections) - len(dropped),
            )
            log.debug(
                "websocket_broadcast",
                ws_event=event,
                recipients=len(connections) - len(dropped),
                org_id=org_id,
            )

    async def send_personal(self, websocket: WebSocket, event: str, data: dict[str, Any]) -> None:
        """Queue an event for a specific client, in order with its broadcasts."""
        message = {
            "event": event,
            "data": data,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        async with self._lock:
            conn = next((c for c in self.active_connections if c.websocket == websocket), None)
        if conn is None:
            # Not registered: answer directly, still bounded.
            try:
                async with asyncio.timeout(self.SEND_TIMEOUT):
                    await websocket.send_json(message)
            except Exception:
                await self.disconnect(websocket)
            return
        if not self._enqueue(conn, message):
            await self._drop(conn, reason="outbox_full")

    def mark_activity(self, websocket: WebSocket) -> None:
        """Mark activity for a connection (e.g., when pong received)."""
        for conn in self.active_connections:
            if conn.websocket == websocket:
                conn.last_activity = datetime.now(UTC)
                conn.last_heartbeat_sent_at = None
                conn.pending_pong = False
                break

    async def subscribe(self, websocket: WebSocket, topics: object) -> list[str]:
        """Restrict a connection to specific broadcast event topics.

        An empty topic list clears the filter, preserving the legacy all-events
        behavior for clients that do not negotiate topics.
        """
        normalized_topics = _normalize_subscription_topics(topics)
        async with self._lock:
            for conn in self.active_connections:
                if conn.websocket == websocket:
                    conn.topics = frozenset(normalized_topics) if normalized_topics else None
                    break
        return normalized_topics

    async def _prepare_heartbeat_batch(
        self,
        *,
        now: datetime,
        send_heartbeats: bool,
    ) -> tuple[list[Connection], list[WebSocket]]:
        """Snapshot heartbeat work while keeping network I/O outside the lock."""
        async with self._lock:
            if not self.active_connections:
                return [], []

            heartbeat_connections: list[Connection] = []
            dead_connections: list[WebSocket] = []
            for conn in self.active_connections:
                if conn.pending_pong:
                    sent_at = conn.last_heartbeat_sent_at or conn.last_activity or now
                    if (now - sent_at).total_seconds() >= self.PONG_TIMEOUT:
                        dead_connections.append(conn.websocket)
                    continue

                if send_heartbeats:
                    conn.pending_pong = True
                    conn.last_heartbeat_sent_at = now
                    heartbeat_connections.append(conn)

        return heartbeat_connections, dead_connections

    async def _heartbeat_loop(self) -> None:
        """Background task that sends heartbeat pings and cleans up dead connections."""
        next_heartbeat_at = datetime.now(UTC) + timedelta(seconds=self.HEARTBEAT_INTERVAL)
        while True:
            now = datetime.now(UTC)
            send_heartbeats = now >= next_heartbeat_at
            heartbeat_connections, dead_connections = await self._prepare_heartbeat_batch(
                now=now,
                send_heartbeats=send_heartbeats,
            )
            if not heartbeat_connections and not dead_connections:
                async with self._lock:
                    if not self.active_connections:
                        log.debug("heartbeat_stopped", reason="no_connections")
                        break

            if send_heartbeats:
                next_heartbeat_at = now + timedelta(seconds=self.HEARTBEAT_INTERVAL)

            heartbeat = {
                "event": "heartbeat",
                "data": {"server_time": now.isoformat()},
                "timestamp": now.isoformat(),
            }
            # Each heartbeat rides the connection's own sender, so one stalled
            # client cannot hold the others' pings past their pong deadline.
            for conn in heartbeat_connections:
                if not self._enqueue(conn, heartbeat):
                    await self._drop(conn, reason="outbox_full")

            # Clean up dead connections
            for ws in dead_connections:
                log.info("heartbeat_timeout", reason="no_pong")
                await self.disconnect(ws)

            await asyncio.sleep(
                min(
                    self.PONG_TIMEOUT,
                    max(0.1, (next_heartbeat_at - datetime.now(UTC)).total_seconds()),
                )
            )


# Global connection manager instance
_manager: ConnectionManager | None = None


def get_manager() -> ConnectionManager:
    """Get or create the global connection manager."""
    global _manager  # noqa: PLW0603
    if _manager is None:
        _manager = ConnectionManager()
    return _manager


async def broadcast_event(event: str, data: dict[str, Any], *, org_id: str | None = None) -> None:
    """Broadcast an event to connected WebSocket clients.

    This is the main interface for other modules to send realtime updates.

    When Redis pub/sub is enabled (multi-pod mode), events are published to Redis
    and each pod forwards them to local connections. Otherwise, broadcasts directly
    to local connections only.

    Args:
        event: Event type name.
        data: Event payload.
        org_id: Organization to broadcast to. If None, broadcasts to all clients.

    Events:
        - entity_created: New entity added
        - entity_updated: Entity modified
        - entity_deleted: Entity removed
        - search_complete: Search finished (for async searches)
        - ingest_progress: Ingestion progress update
        - ingest_complete: Ingestion finished
        - health_update: Server health changed (system-wide, no org filter)
    """
    if _pubsub_enabled:
        # Multi-pod mode: publish to Redis, all pods receive and broadcast locally
        from sibyl.api.pubsub import publish_event

        await publish_event(event, data, org_id=org_id)
    else:
        # Single-pod mode: broadcast directly to local connections
        manager = get_manager()
        await manager.broadcast(event, data, org_id=org_id)


# An entity's own prose. A broadcast reaches connections that may not be
# authorized for the row, so none of these may ride along with the notification.
ENTITY_CONTENT_FIELDS = frozenset(
    {"name", "title", "description", "content", "summary", "metadata"}
)


def entity_change_payload(entity_id: str, entity_type: str, **fields: Any) -> dict[str, Any]:
    """Reduce an entity change to what a broadcast may carry.

    A broadcast fans out to every connection in the organization, and a
    connection is authenticated as an org, not as a reader, so there is no
    identity here to run the scope rule against. Rather than reimplement
    authorization for the fan-out, the channel stops carrying memory: clients
    receive that a row changed and refetch it through the REST endpoint, which
    authorizes them individually.

    Workflow signals a caller passes through (action, status, revision) are
    kept — they are not the entity's prose — while its own text is dropped
    here rather than at each call site.
    """
    payload: dict[str, Any] = {"id": entity_id, "entity_type": entity_type}
    payload.update(
        {key: value for key, value in fields.items() if key not in ENTITY_CONTENT_FIELDS}
    )
    return payload


async def local_broadcast(event: str, data: dict[str, Any], org_id: str | None) -> None:
    """Broadcast to local WebSocket connections only.

    Called by Redis pub/sub listener when a message is received.
    This avoids re-publishing to Redis (which would cause infinite loop).

    Args:
        event: Event type name.
        data: Event payload.
        org_id: Organization to broadcast to.
    """
    manager = get_manager()
    await manager.broadcast(event, data, org_id=org_id)


def enable_pubsub() -> None:
    """Enable Redis pub/sub for multi-pod broadcasts.

    Called during server startup when Redis is available.
    """
    global _pubsub_enabled  # noqa: PLW0603
    _pubsub_enabled = True
    log.info("websocket_pubsub_enabled")


def disable_pubsub() -> None:
    """Disable Redis pub/sub (fallback to local-only broadcasts).

    Called during shutdown or if Redis becomes unavailable.
    """
    global _pubsub_enabled  # noqa: PLW0603
    _pubsub_enabled = False
    log.info("websocket_pubsub_disabled")


def _connection_accepts_event(conn: Connection, event: str) -> bool:
    return conn.topics is None or event in conn.topics


def _normalize_subscription_topics(topics: object) -> list[str]:
    if isinstance(topics, str) or not isinstance(topics, Iterable):
        return []
    valid_topics = {event.value for event in WSEvent}
    normalized = {topic for topic in topics if isinstance(topic, str) and topic in valid_topics}
    return sorted(normalized)


async def _extract_org_from_token(websocket: WebSocket) -> str | None:
    """Extract organization ID from a verified access token.

    WebSocket requests don't pass through HTTP middleware, so we must validate
    the token here. We accept either:
      - Authorization: Bearer <access_token> (non-browser clients)
      - Cookie: sibyl_access_token=<access_token> (browser clients)
    """
    if config_module.settings.disable_auth:
        return None

    claims = await resolve_active_websocket_claims(websocket)
    if claims is None:
        return None

    org_id = claims.get("org")
    return str(org_id) if org_id else None


async def websocket_handler(websocket: WebSocket) -> None:
    """Handle WebSocket connections.

    Extracts org context from auth cookie for scoped broadcasts.
    Server sends heartbeat pings every 30s; clients must respond with pong.

    Clients can send:
        - {"type": "ping"} - Receive pong response
        - {"type": "pong"} or {"type": "heartbeat_ack"} - Acknowledge server heartbeat
        - {"type": "subscribe", "topics": [...]} - Subscribe to specific event types

    Server sends:
        - {"event": "heartbeat", ...} - Keepalive ping (every 30s)
        - Broadcast events scoped to the client's organization
        - Personal responses to client messages
    """
    manager = get_manager()
    org_id = await _extract_org_from_token(websocket)
    if not config_module.settings.disable_auth and not org_id:
        await websocket.accept()
        await websocket.close(code=1008)
        return

    await manager.connect(websocket, org_id=org_id)

    try:
        while True:
            try:
                data = await websocket.receive_json()
                msg_type = data.get("type")

                if msg_type == "ping":
                    await manager.send_personal(websocket, "pong", {})
                    manager.mark_activity(websocket)
                elif msg_type in {"pong", "heartbeat_ack"}:
                    # Client responding to server heartbeat
                    manager.mark_activity(websocket)
                elif msg_type == "subscribe":
                    topics = await manager.subscribe(websocket, data.get("topics", []))
                    await manager.send_personal(websocket, "subscribed", {"topics": topics})
                else:
                    await manager.send_personal(
                        websocket, "error", {"message": f"Unknown message type: {msg_type}"}
                    )
            except ValueError:
                await manager.send_personal(websocket, "error", {"message": "Invalid JSON"})
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(websocket)
