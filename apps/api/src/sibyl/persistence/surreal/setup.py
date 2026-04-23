"""Setup and setup-gating adapters backed by Surreal auth storage."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from starlette.requests import Request

from sibyl.auth.dependencies import build_auth_context
from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import JwtError, verify_access_token
from sibyl.persistence.setup_common import LegacySetupStatus
from sibyl.persistence.surreal.auth import (
    SurrealOrganizationRepository,
    SurrealUserRepository,
    build_surreal_auth_client,
)
from sibyl_core.auth import AuthUser, OrganizationRole

_ADMIN_ROLES = (OrganizationRole.OWNER, OrganizationRole.ADMIN)


async def is_legacy_setup_mode() -> bool:
    """Return whether the system has no users and is still in setup mode."""
    client = build_surreal_auth_client()
    try:
        users = SurrealUserRepository.from_client(client)
        return not await users.has_any_users()
    finally:
        await client.close()


async def get_legacy_setup_status() -> LegacySetupStatus:
    """Return whether Surreal auth storage has users and organizations."""
    client = build_surreal_auth_client()
    try:
        users = SurrealUserRepository.from_client(client)
        orgs = SurrealOrganizationRepository.from_client(client)
        has_users = await users.has_any_users()
        has_orgs = bool(await orgs.list_all(limit=1))
        return LegacySetupStatus(has_users=has_users, has_orgs=has_orgs)
    finally:
        await client.close()


def _require_request_token(request: Request) -> str:
    token = select_access_token(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get("sibyl_access_token"),
    )
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Setup is complete. Authentication required.",
        )
    return token


def _verify_token_claims(token: str) -> dict[str, object]:
    try:
        claims = verify_access_token(token)
    except JwtError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        ) from exc

    if not isinstance(claims, dict):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )
    return claims


async def require_legacy_setup_mode_or_auth(request: Request) -> None:
    """Allow setup mode access, otherwise require a valid access token."""
    if await is_legacy_setup_mode():
        return

    token = _require_request_token(request)
    _verify_token_claims(token)


async def require_legacy_setup_mode_or_admin(request: Request) -> AuthUser | None:
    """Allow setup mode access, otherwise require an authenticated admin."""
    if await is_legacy_setup_mode():
        return None

    token = _require_request_token(request)
    claims = _verify_token_claims(token)

    try:
        UUID(str(claims.get("sub", "")))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: missing user ID",
        ) from exc

    ctx = await build_auth_context(request, None)
    if not ctx.user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required to update server configuration",
        )
    return ctx.user


async def require_legacy_settings_admin(request: Request) -> None:
    """Allow setup-mode bootstrap access, otherwise require an org admin."""
    if await is_legacy_setup_mode():
        return

    ctx = await build_auth_context(request, None)
    if ctx.organization is None or ctx.org_role not in _ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin or owner role required")
