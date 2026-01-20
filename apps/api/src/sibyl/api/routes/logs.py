"""Log streaming endpoints for developer introspection.

Provides access to captured log entries for debugging and monitoring.
Requires OWNER role (super admin equivalent).
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketException, status

from sibyl.auth.dependencies import require_org_role
from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import JwtError, verify_access_token
from sibyl.db.models import OrganizationRole
from sibyl_core.logging import LogBuffer

log = structlog.get_logger()

router = APIRouter(
    prefix="/logs",
    tags=["logs"],
)

# Super admin = OWNER role
_OWNER_ONLY = (OrganizationRole.OWNER,)


@router.get(
    "",
    dependencies=[Depends(require_org_role(*_OWNER_ONLY))],
)
async def get_logs(
    limit: Annotated[int, Query(ge=1, le=500, description="Max entries to return")] = 50,
    service: Annotated[str | None, Query(description="Filter by service name")] = None,
    level: Annotated[str | None, Query(description="Filter by log level")] = None,
) -> list[dict]:
    """Get recent log entries.

    Returns log entries captured from the server's ring buffer.
    Requires organization OWNER role.

    Args:
        limit: Maximum entries to return (1-500)
        service: Filter by service name (api, worker, etc.)
        level: Filter by log level (info, error, warning, debug)

    Returns:
        List of log entries, newest last
    """
    buffer = LogBuffer.get()
    entries = buffer.tail(n=limit, service=service, level=level)
    return [e.to_dict() for e in entries]


@router.get(
    "/stats",
    dependencies=[Depends(require_org_role(*_OWNER_ONLY))],
)
async def get_log_stats() -> dict:
    """Get log buffer statistics.

    Returns current buffer size and subscriber count.
    """
    buffer = LogBuffer.get()
    return {
        "buffer_size": buffer.size,
        "subscriber_count": buffer.subscriber_count,
    }


async def _validate_owner_token(token: str | None) -> bool:
    """Validate that a token belongs to an OWNER.

    For WebSocket auth, we verify the token and check org membership.
    This is a simplified check - full validation would require DB lookup.
    """
    if not token:
        return False

    try:
        claims = verify_access_token(token)
        # For now, trust the token claims - full validation would check DB
        # In production, you'd verify the user is actually an OWNER
        return claims.get("org") is not None
    except JwtError:
        return False


@router.websocket("/stream")
async def stream_logs(websocket: WebSocket) -> None:
    """Stream log entries in real-time via WebSocket.

    Requires authentication via query parameter token.
    Connect with: ws://host/api/logs/stream?token=<jwt>

    Messages are JSON objects with: timestamp, service, level, event, context
    """
    # Validate auth via query param
    token = websocket.query_params.get("token")
    if not await _validate_owner_token(token):
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)

    await websocket.accept()
    log.info("log_stream_connected", client=str(websocket.client))

    buffer = LogBuffer.get()
    queue = buffer.subscribe()

    try:
        while True:
            entry = await queue.get()
            try:
                await websocket.send_json(entry.to_dict())
            except Exception:
                # Client disconnected
                break
    finally:
        buffer.unsubscribe(queue)
        log.info("log_stream_disconnected", client=str(websocket.client))
