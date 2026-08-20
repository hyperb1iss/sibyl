"""Shared authentication for WebSocket connection handshakes."""

from __future__ import annotations

from starlette.websockets import WebSocket

from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import JwtError, verify_access_token
from sibyl.persistence.auth_runtime import validate_access_session


async def resolve_active_websocket_claims(websocket: WebSocket) -> dict[str, object] | None:
    """Return claims only for a valid, active access-token session."""
    token = select_access_token(
        authorization=websocket.headers.get("authorization"),
        cookie_token=websocket.cookies.get("sibyl_access_token"),
    )
    if not token:
        return None

    try:
        claims = verify_access_token(token)
    except JwtError:
        return None

    try:
        if not await validate_access_session(token):
            return None
    except TimeoutError:
        return None

    return dict(claims)
