"""WebSocket connection manager for realtime updates.

Broadcasts entity changes, search results, and system events to all connected clients.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog
from starlette.websockets import WebSocket, WebSocketDisconnect

log = structlog.get_logger()


class ConnectionManager:
    """Manages WebSocket connections and broadcasts events."""

    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and register a new WebSocket connection."""
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)
        log.info("websocket_connected", total_connections=len(self.active_connections))

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection."""
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)
        log.info("websocket_disconnected", total_connections=len(self.active_connections))

    async def broadcast(self, event: str, data: dict[str, Any]) -> None:
        """Broadcast an event to all connected clients."""
        if not self.active_connections:
            return

        message = {
            "event": event,
            "data": data,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        # Copy list to avoid modification during iteration
        async with self._lock:
            connections = list(self.active_connections)

        disconnected = []
        for connection in connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)

        # Clean up disconnected clients
        for conn in disconnected:
            await self.disconnect(conn)

        if connections:
            log.debug(
                "websocket_broadcast",
                event=event,
                recipients=len(connections) - len(disconnected),
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


async def broadcast_event(event: str, data: dict[str, Any]) -> None:
    """Broadcast an event to all connected WebSocket clients.

    This is the main interface for other modules to send realtime updates.

    Events:
        - entity_created: New entity added
        - entity_updated: Entity modified
        - entity_deleted: Entity removed
        - search_complete: Search finished (for async searches)
        - ingest_progress: Ingestion progress update
        - ingest_complete: Ingestion finished
        - health_update: Server health changed
    """
    manager = get_manager()
    await manager.broadcast(event, data)


async def websocket_handler(websocket: WebSocket) -> None:
    """Handle WebSocket connections.

    Clients can send:
        - {"type": "ping"} - Receive pong response
        - {"type": "subscribe", "topics": [...]} - Subscribe to specific event types

    Server sends:
        - All broadcast events (entity changes, etc.)
        - Personal responses to client messages
    """
    manager = get_manager()
    await manager.connect(websocket)

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
