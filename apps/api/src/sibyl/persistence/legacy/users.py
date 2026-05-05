"""Legacy user adapters backed by the current relational runtime."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl.db.connection import get_session
from sibyl.db.models import OAuthConnection, User
from sibyl.email import get_email_client
from sibyl.persistence.legacy.auth_managers.password_reset import (
    PasswordResetError,
    PasswordResetManager,
)


async def request_password_reset(email: str) -> None:
    """Request a password reset using the current relational auth runtime."""
    async with get_session() as session:
        manager = PasswordResetManager(session, get_email_client())
        await manager.request_reset(email)


async def confirm_password_reset(token: str, new_password: str) -> None:
    """Confirm a password reset using the current relational auth runtime."""
    async with get_session() as session:
        manager = PasswordResetManager(session, get_email_client())
        try:
            await manager.confirm_reset(token, new_password)
        except PasswordResetError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


async def list_oauth_connections(session: AsyncSession, user_id: UUID) -> list[OAuthConnection]:
    """List OAuth connections for a user from the relational runtime."""
    result = await session.execute(
        select(OAuthConnection).where(OAuthConnection.user_id == user_id)
    )
    return list(result.scalars().all())


async def remove_oauth_connection(
    session: AsyncSession,
    *,
    user_id: UUID,
    connection_id: UUID,
) -> OAuthConnection:
    """Remove an OAuth connection, keeping at least one login method."""
    result = await session.execute(
        select(OAuthConnection)
        .where(OAuthConnection.id == connection_id)
        .where(OAuthConnection.user_id == user_id)
    )
    connection = result.scalar_one_or_none()

    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    user = await session.get(User, user_id)

    remaining = await session.execute(
        select(OAuthConnection)
        .where(OAuthConnection.user_id == user_id)
        .where(OAuthConnection.id != connection_id)
    )
    has_other_connections = remaining.scalar_one_or_none() is not None
    has_password = bool(user and user.password_hash)

    if not has_other_connections and not has_password:
        raise HTTPException(
            status_code=400,
            detail="Cannot remove last login method. Set a password first.",
        )

    await session.delete(connection)
    await session.commit()
    return connection
