"""Setup and setup-gating adapters backed by Surreal auth storage."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from uuid import UUID

from fastapi import HTTPException, status
from starlette.requests import Request

from sibyl.auth.dependencies import build_auth_context
from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import JwtError, verify_access_token
from sibyl.persistence.setup_common import SetupStatus
from sibyl.persistence.surreal.auth import (
    SurrealOrganizationRepository,
    SurrealUserRepository,
    build_surreal_auth_client,
)
from sibyl_core.auth import AuthUser, OrganizationRole

_ADMIN_ROLES = (OrganizationRole.OWNER, OrganizationRole.ADMIN)
_SETUP_COMPLETE_DETAIL = {
    "code": "setup_already_initialized",
    "message": "Setup is complete. Sign in with an owner or admin account.",
}


def _object_mapping(value: object) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        return {}
    return cast("Mapping[object, object]", value)


def _has_records(value: object) -> bool:
    if isinstance(value, list):
        return bool(value)
    return isinstance(value, dict)


async def is_setup_mode() -> bool:
    """Return whether the system is still in setup mode."""
    return not (await get_setup_status()).setup_complete


async def get_setup_status() -> SetupStatus:
    """Return whether Surreal auth storage has users and organizations."""
    client = build_surreal_auth_client()
    try:
        payload = _object_mapping(
            await client.execute_query(
                """
                    RETURN {
                        users: (SELECT uuid FROM users LIMIT 1),
                        organizations: (SELECT uuid FROM organizations LIMIT 1),
                        initialized_memberships: (
                            SELECT uuid FROM organization_members
                            WHERE role = $owner_role OR role = $admin_role
                            LIMIT 1
                        ),
                    };
                """,
                owner_role=OrganizationRole.OWNER.value,
                admin_role=OrganizationRole.ADMIN.value,
            )
        )
        return SetupStatus(
            has_users=_has_records(payload.get("users")),
            has_orgs=_has_records(payload.get("organizations")),
            setup_complete=_has_records(payload.get("initialized_memberships")),
        )
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
            detail=_SETUP_COMPLETE_DETAIL,
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


async def require_setup_mode_or_auth(request: Request) -> None:
    """Allow setup mode access, otherwise require a valid access token."""
    if await is_setup_mode():
        return

    token = _require_request_token(request)
    _verify_token_claims(token)


async def require_setup_mode_or_admin(request: Request) -> AuthUser | None:
    """Allow setup mode access, otherwise require an authenticated global admin."""
    if await is_setup_mode():
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
    if ctx.user.is_admin is not True:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Global admin required",
        )
    return ctx.user


async def require_settings_admin(request: Request) -> None:
    """Allow setup-mode bootstrap access, otherwise require an org admin."""
    if await is_setup_mode():
        return

    ctx = await build_auth_context(request, None)
    if ctx.organization is None or ctx.org_role not in _ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin or owner role required")


async def require_settings_owner(request: Request) -> None:
    """Allow setup-mode bootstrap access, otherwise require a global admin.

    Org owner/admin is insufficient: every user owns a personal
    organization with the OWNER role, so an org-scoped check gates
    nothing for instance-wide settings.
    """
    if await is_setup_mode():
        return

    ctx = await build_auth_context(request, None)
    if ctx.user.is_admin is not True:
        raise HTTPException(status_code=403, detail="Global admin required")


__all__ = [
    "SetupStatus",
    "SurrealOrganizationRepository",
    "SurrealUserRepository",
    "build_surreal_auth_client",
    "get_setup_status",
    "is_setup_mode",
    "require_settings_admin",
    "require_settings_owner",
    "require_setup_mode_or_admin",
    "require_setup_mode_or_auth",
]
