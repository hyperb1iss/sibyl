"""FastAPI auth dependencies."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status

from sibyl.auth.context import AuthContext
from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import JwtError, verify_access_token
from sibyl.config import settings
from sibyl.db.models import OrganizationRole
from sibyl.persistence.auth_runtime import (
    InvalidAuthClaimsError,
    UserNotFoundError,
    authenticate_api_key,
    get_user_by_id,
    resolve_auth_context,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from sibyl.db.models import User

_logger = logging.getLogger(__name__)

# API key scope enforcement for REST.
#
# API keys are intended for least-privilege automation. For REST usage, we enforce:
# - Safe methods (GET/HEAD/OPTIONS): require api:read OR api:write
# - Mutating methods: require api:write
_SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_REST_READ_SCOPES = frozenset({"api:read", "api:write"})
_REST_WRITE_SCOPE = "api:write"

# Security warning at startup if auth is disabled
if settings.disable_auth:
    _logger.warning(
        "SECURITY WARNING: Authentication is DISABLED (SIBYL_DISABLE_AUTH=true). "
        "This should only be used for local development. Environment: %s",
        settings.environment,
    )


def _is_rest_request(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def _api_key_allows_rest(*, scopes: list[str], method: str) -> bool:
    normalized = {s.strip() for s in scopes if str(s).strip()}
    if method.upper() in _SAFE_HTTP_METHODS:
        return bool(normalized & _REST_READ_SCOPES)
    return _REST_WRITE_SCOPE in normalized


async def resolve_claims(request: Request, _session: AsyncSession | None = None) -> dict | None:
    claims = getattr(request.state, "jwt_claims", None)
    if claims:
        return claims

    token = select_access_token(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get("sibyl_access_token"),
    )
    if not token:
        return None

    try:
        return verify_access_token(token)
    except JwtError:
        pass

    if token.startswith("sk_"):
        auth = await authenticate_api_key(token)
        if auth:
            scopes = list(auth.scopes or [])
            if _is_rest_request(request) and not _api_key_allows_rest(
                scopes=scopes, method=request.method
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Insufficient API key scope",
                )
            return {
                "sub": str(auth.user_id),
                "org": str(auth.organization_id),
                "typ": "api_key",
                "scopes": scopes,
            }

    return None


async def get_current_user(
    request: Request,
) -> User:
    claims = await resolve_claims(request)
    if not claims:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    try:
        user_id = UUID(str(claims.get("sub", "")))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from e

    cached_ctx = getattr(request.state, "auth_context", None)
    cached_user = getattr(cached_ctx, "user", None)
    if getattr(cached_user, "id", None) == user_id:
        return cast("User", cached_user)

    user = await get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


async def get_current_organization(
    request: Request,
) -> Any:
    ctx = await build_auth_context(request)
    if ctx.organization is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return ctx.organization


async def get_current_org_role(
    request: Request,
) -> OrganizationRole:
    ctx = await build_auth_context(request)
    if ctx.org_role is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a member")
    return ctx.org_role


def require_org_role(*allowed: OrganizationRole):
    async def _check_role(role: OrganizationRole = Depends(get_current_org_role)) -> None:
        if role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    async def _noop() -> None:
        pass

    if settings.disable_auth:
        return _noop
    return _check_role


async def build_auth_context(
    request: Request,
    session=None,
) -> AuthContext:
    """Build AuthContext from request. Standalone function for direct calls.

    This is the core implementation used by both FastAPI dependency injection
    and direct calls from other auth modules (e.g., rls.py).
    """
    if session is None:
        cached = getattr(request.state, "auth_context", None)
        if cached is not None:
            return cached

    claims = await resolve_claims(request)
    if not claims:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        ctx = await resolve_auth_context(claims=claims, session=session)
    except InvalidAuthClaimsError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from e
    except UserNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found"
        ) from e
    if session is None:
        request.state.auth_context = ctx
    return ctx


async def get_auth_context(
    request: Request,
) -> AuthContext:
    """FastAPI dependency wrapper for build_auth_context."""
    return await build_auth_context(request)


def require_org_admin():
    async def _check_admin(ctx: AuthContext = Depends(get_auth_context)) -> None:
        if ctx.organization is None or ctx.org_role not in {
            OrganizationRole.OWNER,
            OrganizationRole.ADMIN,
        }:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    async def _noop() -> None:
        pass

    if settings.disable_auth:
        return _noop
    return _check_admin
