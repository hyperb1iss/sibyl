"""FastAPI REST API for Sibyl web frontend.

Provides REST endpoints at /api/* alongside MCP at /mcp.
Includes WebSocket support for realtime updates.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sibyl.api.app import create_api_app
    from sibyl.api.websocket import ConnectionManager, broadcast_event

__all__ = ["create_api_app", "broadcast_event", "ConnectionManager"]

_LAZY_EXPORTS = {
    "create_api_app": ("sibyl.api.app", "create_api_app"),
    "broadcast_event": ("sibyl.api.websocket", "broadcast_event"),
    "ConnectionManager": ("sibyl.api.websocket", "ConnectionManager"),
}


def __getattr__(name: str) -> Any:
    module_name, attr_name = _LAZY_EXPORTS.get(name, (None, None))
    if not module_name or not attr_name:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
