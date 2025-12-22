"""FastAPI/Starlette auth middleware."""

from __future__ import annotations

from typing import Any

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import JwtError, verify_access_token

log = structlog.get_logger()


class AuthMiddleware(BaseHTTPMiddleware):
    """Parse bearer tokens and attach decoded JWT claims to request.state."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        request.state.jwt_claims = None
        token = select_access_token(
            authorization=request.headers.get("authorization"),
            cookie_token=request.cookies.get("sibyl_access_token"),
        )
        if token:
            try:
                request.state.jwt_claims = verify_access_token(token)
            except JwtError as e:
                log.debug("Invalid bearer token", error=str(e))
                request.state.jwt_claims = None

        response = await call_next(request)
        return response
