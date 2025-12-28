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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from starlette.websockets import WebSocket, WebSocketDisconnect

from sibyl import config as config_module
from sibyl.auth.http import extract_bearer_token
from sibyl.auth.jwt import JwtError, verify_access_token

log = structlog.get_logger()

# Flag to track if Redis pub/sub is available
_pubsub_enabled: bool = False


@dataclass
class Connection:
    """WebSocket connection with org context."""

    websocket: WebSocket
    org_id: str | None = None
    last_activity: datetime | None = None
    pending_pong: bool = False


class ConnectionManager:
    """Manages WebSocket connections and broadcasts events by organization."""

    # Heartbeat interval in seconds
    HEARTBEAT_INTERVAL = 30
    # How long to wait for pong before considering connection dead
    PONG_TIMEOUT = 10

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
            # Start heartbeat task if not running
            if self._heartbeat_task is None or self._heartbeat_task.done():
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        log.info(
            "websocket_connected",
            total_connections=len(self.active_connections),
            org_id=org_id,
        )

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection."""
        async with self._lock:
            self.active_connections = [
                c for c in self.active_connections if c.websocket != websocket
            ]
        log.info("websocket_disconnected", total_connections=len(self.active_connections))

    async def broadcast(self, event: str, data: dict[str, Any], org_id: str | None = None) -> None:
        """Broadcast an event to clients in the same organization.

        Args:
            event: Event type name.
            data: Event payload.
            org_id: If provided, only broadcast to clients in this org.
                   If None, broadcast to all clients (system events).
        """
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
                connections = [c for c in self.active_connections if c.org_id == org_id]
            else:
                connections = list(self.active_connections)

        disconnected = []
        for conn in connections:
            try:
                await conn.websocket.send_json(message)
            except Exception:
                disconnected.append(conn.websocket)

        # Clean up disconnected clients
        for ws in disconnected:
            await self.disconnect(ws)

        if connections:
            log.debug(
                "websocket_broadcast",
                ws_event=event,
                recipients=len(connections) - len(disconnected),
                org_id=org_id,
            )

    async def send_personal(self, websocket: WebSocket, event: str, data: dict[str, Any]) -> None:
        """Send an event to a specific client."""
        message = {
            "event": event,
            "data": data,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        try:
            await websocket.send_json(message)
        except Exception:
            await self.disconnect(websocket)

    def mark_activity(self, websocket: WebSocket) -> None:
        """Mark activity for a connection (e.g., when pong received)."""
        for conn in self.active_connections:
            if conn.websocket == websocket:
                conn.last_activity = datetime.now(UTC)
                conn.pending_pong = False
                break

    async def _heartbeat_loop(self) -> None:
        """Background task that sends heartbeat pings and cleans up dead connections."""
        while True:
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)

            if not self.active_connections:
                log.debug("heartbeat_stopped", reason="no_connections")
                break

            dead_connections: list[WebSocket] = []
            now = datetime.now(UTC)

            async with self._lock:
                for conn in self.active_connections:
                    # Check if previous ping was answered
                    if conn.pending_pong:
                        # No pong received - connection is dead
                        dead_connections.append(conn.websocket)
                        continue

                    # Send heartbeat ping
                    try:
                        await conn.websocket.send_json(
                            {
                                "event": "heartbeat",
                                "data": {"server_time": now.isoformat()},
                                "timestamp": now.isoformat(),
                            }
                        )
                        conn.pending_pong = True
                    except Exception:
                        dead_connections.append(conn.websocket)

            # Clean up dead connections
            for ws in dead_connections:
                log.info("heartbeat_timeout", reason="no_pong")
                await self.disconnect(ws)


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


def _extract_org_from_token(websocket: WebSocket) -> str | None:
    """Extract organization ID from a verified access token.

    WebSocket requests don't pass through HTTP middleware, so we must validate
    the token here. We accept either:
      - Authorization: Bearer <access_token> (non-browser clients)
      - Cookie: sibyl_access_token=<access_token> (browser clients)
    """
    if config_module.settings.disable_auth:
        return None

    auth_header = websocket.headers.get("authorization")
    token = extract_bearer_token(auth_header) or websocket.cookies.get("sibyl_access_token")
    if not token:
        return None

    try:
        claims = verify_access_token(token)
    except JwtError:
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
    org_id = _extract_org_from_token(websocket)
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
                    # For now, all clients receive all events
                    # Future: implement topic-based filtering
                    topics = data.get("topics", [])
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
