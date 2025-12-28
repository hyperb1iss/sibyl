"""FastAPI REST API for Sibyl web frontend.

Provides REST endpoints at /api/* alongside MCP at /mcp.
Includes WebSocket support for realtime updates.
"""

from sibyl.api.app import create_api_app
from sibyl.api.websocket import ConnectionManager, broadcast_event

__all__ = ["create_api_app", "broadcast_event", "ConnectionManager"]
