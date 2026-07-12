"""FastAPI auth dependencies."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
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
_MEMORY_PROVIDER_PROFILE = "memory_provider"
_MEMORY_PROVIDER_ENDPOINTS = frozenset(
    {
        ("GET", "/api/auth/me"),
        ("POST", "/api/context/pack"),
        ("POST", "/api/memory/expose"),
        ("POST", "/api/memory/raw"),
    }
)
_MEMORY_PROVIDER_CORRECTION_PATH = re.compile(r"^/api/memory/inspect/.+/corrections(?:/preview)?$")
_MEMORY_PROVIDER_CORRECTION_ACTIONS = frozenset(
    {
        "hide",
        "mark_duplicate",
        "mark_sensitive",
        "mark_stale",
        "mark_wrong",
        "restore",
        "revise",
        "supersede",
    }
)

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


def _insufficient_api_scope(*, scopes: list[str], method: str) -> HTTPException:
    expected = "api:read or api:write" if method.upper() in _SAFE_HTTP_METHODS else "api:write"
    actual = ", ".join(scope for scope in scopes if scope.strip()) or "none"
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "insufficient_api_scope",
            "message": "Request is missing required REST scope.",
            "remediation": "Use a REST scope that matches this request.",
            "details": {
                "expected": expected,
                "actual": actual,
            },
        },
    )


def _capability_profile_forbidden(*, method: str, path: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "capability_profile_forbidden",
            "message": "This API key cannot access the requested operation.",
            "details": {"method": method.upper(), "path": path},
        },
    )


async def _request_body_mapping(request: Request) -> Mapping[str, object] | None:
    try:
        body = await request.json()
    except (UnicodeDecodeError, ValueError):
        return None
    return body if isinstance(body, Mapping) else None


async def _enforce_memory_provider_profile(request: Request, auth: ApiKeyAuth) -> None:
    if getattr(auth, "capability_profile", None) != _MEMORY_PROVIDER_PROFILE:
        return

    method = request.method.upper()
    path = request.url.path.rstrip("/") or "/"
    if (method, path) in _MEMORY_PROVIDER_ENDPOINTS:
        pass
    elif method == "POST" and _MEMORY_PROVIDER_CORRECTION_PATH.fullmatch(path):
        body = await _request_body_mapping(request)
        action = str(body.get("action") or "") if body is not None else ""
        if action not in _MEMORY_PROVIDER_CORRECTION_ACTIONS:
            raise _capability_profile_forbidden(method=method, path=path)
    else:
        raise _capability_profile_forbidden(method=method, path=path)

    agent_id = getattr(auth, "agent_id", None)
    if agent_id is None or method != "POST":
        return
    body = await _request_body_mapping(request)
    request_agent_id = body.get("agent_id") if body is not None else None
    if request_agent_id is not None and str(request_agent_id) != agent_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "agent_identity_conflict",
                "message": "Request agent identity conflicts with the authenticated API key.",
            },
        )


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
    if agent_id := getattr(auth, "agent_id", None):
        claims["agent_id"] = agent_id
    if delegated_authority := getattr(auth, "delegated_authority", None):
        claims["delegated_authority"] = delegated_authority
    if capability_profile := getattr(auth, "capability_profile", None):
        claims["capability_profile"] = capability_profile
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
                    raise _insufficient_api_scope(scopes=scopes, method=request.method)
                await _enforce_memory_provider_profile(request, auth)
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
