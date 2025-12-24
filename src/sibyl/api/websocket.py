"""WebSocket connection manager for realtime updates.

Broadcasts entity changes, search results, and system events to connected clients.
Scoped by organization for multi-tenant security.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from starlette.websockets import WebSocket, WebSocketDisconnect

log = structlog.get_logger()


@dataclass
class Connection:
    """WebSocket connection with org context."""

    websocket: WebSocket
    org_id: str | None = None


class ConnectionManager:
    """Manages WebSocket connections and broadcasts events by organization."""

    def __init__(self) -> None:
        self.active_connections: list[Connection] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, org_id: str | None = None) -> None:
        """Accept and register a new WebSocket connection with org context."""
        await websocket.accept()
        conn = Connection(websocket=websocket, org_id=org_id)
        async with self._lock:
            self.active_connections.append(conn)
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
    manager = get_manager()
    await manager.broadcast(event, data, org_id=org_id)


def _extract_org_from_token(websocket: WebSocket) -> str | None:
    """Extract organization ID from the auth cookie JWT."""
    import base64
    import json

    cookie = websocket.cookies.get("sibyl_access_token")
    if not cookie:
        return None

    try:
        # JWT format: header.payload.signature
        payload = cookie.split(".")[1]
        # Add padding if needed
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.b64decode(payload)
        claims = json.loads(decoded)
        return claims.get("org")
    except Exception:
        return None


async def websocket_handler(websocket: WebSocket) -> None:
    """Handle WebSocket connections.

    Extracts org context from auth cookie for scoped broadcasts.

    Clients can send:
        - {"type": "ping"} - Receive pong response
        - {"type": "subscribe", "topics": [...]} - Subscribe to specific event types

    Server sends:
        - Broadcast events scoped to the client's organization
        - Personal responses to client messages
    """
    manager = get_manager()
    org_id = _extract_org_from_token(websocket)
    await manager.connect(websocket, org_id=org_id)

    try:
        while True:
            try:
                data = await websocket.receive_json()
                msg_type = data.get("type")

                if msg_type == "ping":
                    await manager.send_personal(websocket, "pong", {})
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
