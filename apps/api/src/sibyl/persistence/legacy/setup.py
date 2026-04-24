"""Legacy setup adapters backed by the relational runtime."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlmodel import select
from starlette.requests import Request

from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import JwtError, verify_access_token
from sibyl.db.connection import get_session
from sibyl.db.models import Organization, User
from sibyl.persistence.setup_common import SetupStatus


async def is_setup_mode() -> bool:
    """Return whether the system has no users and is still in setup mode."""
    async with get_session() as session:
        result = await session.execute(select(func.count(User.id)))
        return (result.scalar() or 0) == 0


async def get_setup_status() -> SetupStatus:
    """Return whether legacy relational storage has users and organizations."""
    async with get_session() as session:
        user_result = await session.execute(select(func.count(User.id)))
        org_result = await session.execute(select(func.count(Organization.id)))
        return SetupStatus(
            has_users=(user_result.scalar() or 0) > 0,
            has_orgs=(org_result.scalar() or 0) > 0,
        )


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


async def require_setup_mode_or_auth(request: Request) -> None:
    """Allow setup mode access, otherwise require a valid access token."""
    if await is_setup_mode():
        return

    token = _require_request_token(request)
    _verify_token_claims(token)


async def require_setup_mode_or_admin(request: Request) -> User | None:
    """Allow setup mode access, otherwise require an authenticated admin."""
    if await is_setup_mode():
        return None

    token = _require_request_token(request)
    claims = _verify_token_claims(token)

    try:
        user_id = UUID(str(claims.get("sub", "")))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: missing user ID",
        ) from exc

    async with get_session() as session:
        user = await session.get(User, user_id)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required to update server configuration",
        )

    return user


async def require_settings_admin(request: Request) -> None:
    from sibyl.persistence.legacy import settings as legacy_settings

    await legacy_settings.require_legacy_settings_admin(request)


is_legacy_setup_mode = is_setup_mode
get_legacy_setup_status = get_setup_status
require_legacy_setup_mode_or_auth = require_setup_mode_or_auth
require_legacy_setup_mode_or_admin = require_setup_mode_or_admin
require_legacy_settings_admin = require_settings_admin

__all__ = [
    "SetupStatus",
    "get_setup_status",
    "get_legacy_setup_status",
    "is_setup_mode",
    "is_legacy_setup_mode",
    "require_settings_admin",
    "require_legacy_settings_admin",
    "require_setup_mode_or_admin",
    "require_legacy_setup_mode_or_admin",
    "require_setup_mode_or_auth",
    "require_legacy_setup_mode_or_auth",
]
