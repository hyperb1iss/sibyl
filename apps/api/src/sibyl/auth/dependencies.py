"""FastAPI auth dependencies."""

from __future__ import annotations

import logging
from typing import cast
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status

from sibyl.auth.api_key_common import ApiKeyAuth
from sibyl.auth.context import AuthContext
from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import JwtError, verify_access_token
from sibyl.config import settings
from sibyl.persistence.auth_runtime import (
    InvalidAuthClaimsError,
    UserNotFoundError,
    authenticate_api_key,
    get_user_by_id,
    resolve_auth_context,
    validate_access_session,
)
from sibyl_core.ai.llm.budget import set_llm_budget_context
from sibyl_core.auth import AuthOrganization, AuthUser, OrganizationRole

_logger = logging.getLogger(__name__)

# API key scope enforcement for REST.
#
# API keys are intended for least-privilege automation. For REST usage, we enforce:
# - Safe methods (GET/HEAD/OPTIONS): require api:read OR api:write
# - Mutating methods: require api:write
_SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_REST_READ_SCOPES = frozenset({"api:read", "api:write"})
_REST_WRITE_SCOPE = "api:write"
_VALIDATED_AUTH_CLAIMS_ATTR = "validated_auth_claims"

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


def _api_key_claims(auth: ApiKeyAuth, *, scopes: list[str]) -> dict[str, object]:
    claims: dict[str, object] = {
        "sub": str(auth.user_id),
        "org": str(auth.organization_id),
        "typ": "api_key",
        "api_key_id": str(auth.api_key_id),
        "scopes": scopes,
    }
    if auth.project_ids is not None:
        claims["api_key_project_ids"] = [str(project_id) for project_id in auth.project_ids]
    if auth.memory_space_ids is not None:
        claims["api_key_memory_space_ids"] = [
            str(memory_space_id) for memory_space_id in auth.memory_space_ids
        ]
    if auth.memory_spaces is not None:
        claims["api_key_memory_scope_keys"] = [space.policy_key for space in auth.memory_spaces]
    return claims


async def resolve_claims(
    request: Request, _session: object | None = None
) -> dict[str, object] | None:
    cached_claims = getattr(request.state, _VALIDATED_AUTH_CLAIMS_ATTR, None)
    if isinstance(cached_claims, dict):
        return cast("dict[str, object]", cached_claims)

    claims = getattr(request.state, "jwt_claims", None)

    token = select_access_token(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get("sibyl_access_token"),
    )
    if token:
        verified_claims = claims
        if verified_claims is None:
            try:
                verified_claims = verify_access_token(token)
            except JwtError:
                verified_claims = None
        if verified_claims is not None:
            try:
                if await validate_access_session(token):
                    resolved_claims = cast("dict[str, object]", verified_claims)
                    setattr(request.state, _VALIDATED_AUTH_CLAIMS_ATTR, resolved_claims)
                    return resolved_claims
            except TimeoutError as e:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Authentication storage temporarily unavailable",
                ) from e
            return None

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
                api_key_claims = _api_key_claims(auth, scopes=scopes)
                setattr(request.state, _VALIDATED_AUTH_CLAIMS_ATTR, api_key_claims)
                return api_key_claims

        return None

    return cast("dict[str, object] | None", claims)


async def get_current_user(
    request: Request,
) -> AuthUser:
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
        return cast("AuthUser", cached_user)

    if cached_ctx is not None:
        request.state.auth_context = None

    if claims.get("org"):
        return (await build_auth_context(request)).user

    try:
        user = await get_user_by_id(user_id)
    except TimeoutError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication storage temporarily unavailable",
        ) from e
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


async def get_current_organization(
    request: Request,
) -> AuthOrganization:
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
    except TimeoutError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication storage temporarily unavailable",
        ) from e
    except InvalidAuthClaimsError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from e
    except UserNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found"
        ) from e
    organization = getattr(ctx, "organization", None)
    set_llm_budget_context(
        user_id=str(ctx.user.id),
        organization_id=str(organization.id) if organization else None,
    )
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
