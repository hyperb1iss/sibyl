"""Log streaming endpoints for developer introspection.

Provides access to captured log entries for debugging and monitoring.
Requires global admin access.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketException, status

from sibyl.auth.jwt import JwtError, verify_access_token
from sibyl.config import settings
from sibyl.persistence.auth_runtime import get_user_by_id, validate_access_session
from sibyl.persistence.operations_runtime import require_global_admin
from sibyl_core.logging import LogBuffer

log = structlog.get_logger()

router = APIRouter(
    prefix="/logs",
    tags=["logs"],
)


@router.get(
    "",
    dependencies=[Depends(require_global_admin)],
)
async def get_logs(
    limit: Annotated[int, Query(ge=1, le=500, description="Max entries to return")] = 50,
    service: Annotated[str | None, Query(description="Filter by service name")] = None,
    level: Annotated[str | None, Query(description="Filter by log level")] = None,
) -> list[dict]:
    """Get recent log entries.

    Returns log entries captured from the server's ring buffer.
    Requires global admin access.

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
    dependencies=[Depends(require_global_admin)],
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
    """Validate that a token belongs to a global admin."""
    if settings.disable_auth:
        return True

    if not token:
        return False

    try:
        claims = verify_access_token(token)
    except JwtError:
        return False
    try:
        is_active = await validate_access_session(token)
    except TimeoutError:
        return False
    if not is_active:
        return False

    try:
        user_id = UUID(str(claims.get("sub", "")))
    except ValueError:
        return False
    try:
        user = await get_user_by_id(user_id)
    except TimeoutError:
        return False

    return bool(user and user.is_admin)


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
