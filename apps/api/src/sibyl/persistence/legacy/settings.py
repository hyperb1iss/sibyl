"""Legacy settings auth adapters backed by the current relational runtime."""

from __future__ import annotations

from sqlalchemy import func
from sqlmodel import select
from starlette.requests import Request

from sibyl.auth.dependencies import build_auth_context
from sibyl.db.connection import get_session
from sibyl.db.models import User
from sibyl_core.auth import OrganizationRole

_ADMIN_ROLES = (OrganizationRole.OWNER, OrganizationRole.ADMIN)


async def is_setup_mode() -> bool:
    """Return whether the system has no users and is in setup mode."""
    async with get_session() as session:
        result = await session.execute(select(func.count(User.id)))
        user_count = result.scalar() or 0
        return user_count == 0


async def require_settings_admin(request: Request) -> None:
    """Allow setup-mode bootstrap access, otherwise require an org admin."""
    if await is_setup_mode():
        return

    async with get_session() as session:
        ctx = await build_auth_context(request, session)
        if ctx.organization is None or ctx.org_role not in _ADMIN_ROLES:
            from fastapi import HTTPException

            raise HTTPException(status_code=403, detail="Admin or owner role required")


is_legacy_setup_mode = is_setup_mode
require_legacy_settings_admin = require_settings_admin

__all__ = [
    "is_setup_mode",
    "is_legacy_setup_mode",
    "require_settings_admin",
    "require_legacy_settings_admin",
]
