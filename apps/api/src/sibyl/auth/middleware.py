"""FastAPI/Starlette auth middleware."""

from __future__ import annotations

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import JwtError, verify_access_token

log = structlog.get_logger()

# Set on request.state once this middleware has run the JWT check, so the
# dependencies never decode the same bearer a second time: claims of None
# then means "verified and rejected", not "not verified yet".
JWT_CHECKED_STATE_ATTR = "jwt_checked"


class AuthMiddleware(BaseHTTPMiddleware):
    """Parse bearer tokens and attach decoded JWT claims to request.state."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        request.state.jwt_claims = None
        token = select_access_token(
            authorization=request.headers.get("authorization"),
            cookie_token=request.cookies.get("sibyl_access_token"),
        )
        # API keys are never JWTs; decoding them only produced two failed
        # verifies and a log line per request before the sk_ branch ran.
        if token and not token.startswith("sk_"):
            try:
                request.state.jwt_claims = verify_access_token(token)
            except JwtError as e:
                log.debug("Invalid bearer token", error=str(e))
                request.state.jwt_claims = None
        setattr(request.state, JWT_CHECKED_STATE_ATTR, True)

        return await call_next(request)
