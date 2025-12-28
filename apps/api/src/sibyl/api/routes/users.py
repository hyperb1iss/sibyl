"""User profile and settings API routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context
from sibyl.auth.http import select_access_token
from sibyl.auth.passwords import hash_password, verify_password
from sibyl.auth.sessions import SessionManager
from sibyl.db.connection import get_session_dependency
from sibyl.db.models import OAuthConnection, User

log = structlog.get_logger()

router = APIRouter(prefix="/users", tags=["users"])


# ============================================================================
# Schemas
# ============================================================================


class UserProfileResponse(BaseModel):
    """User profile response."""

    id: UUID
    email: str | None
    name: str | None
    bio: str | None
    timezone: str | None
    avatar_url: str | None
    email_verified_at: datetime | None
    created_at: datetime


class ProfileUpdateRequest(BaseModel):
    """Profile update request."""

    name: str | None = Field(None, max_length=100)
    bio: str | None = Field(None, max_length=500)
    timezone: str | None = Field(None, max_length=50)
    avatar_url: str | None = Field(None, max_length=500_000)  # Supports data URLs


class PreferencesResponse(BaseModel):
    """User preferences response."""

    preferences: dict[str, Any]


class PreferencesUpdateRequest(BaseModel):
    """Preferences update request."""

    preferences: dict[str, Any]


class PasswordChangeRequest(BaseModel):
    """Password change request."""

    current_password: str
    new_password: str = Field(..., min_length=8, max_length=128)


class PasswordResetRequest(BaseModel):
    """Password reset request."""

    email: EmailStr


class PasswordResetConfirmRequest(BaseModel):
    """Password reset confirmation."""

    token: str
    new_password: str = Field(..., min_length=8, max_length=128)


class SessionResponse(BaseModel):
    """Session info response."""

    id: UUID
    user_agent: str | None
    ip_address: str | None
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    is_current: bool


class OAuthConnectionResponse(BaseModel):
    """OAuth connection response."""

    id: UUID
    provider: str
    provider_user_id: str
    provider_email: str | None
    connected_at: datetime


# ============================================================================
# Profile Endpoints
# ============================================================================


@router.get("/me/profile", response_model=UserProfileResponse)
async def get_profile(
    auth: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
) -> UserProfileResponse:
    """Get current user's profile."""
    user = await session.get(User, auth.user.id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return UserProfileResponse(
        id=user.id,
        email=user.email,
        name=user.name,
        bio=user.bio,
        timezone=user.timezone,
        avatar_url=user.avatar_url,
        email_verified_at=user.email_verified_at,
        created_at=user.created_at,
    )


@router.patch("/me/profile", response_model=UserProfileResponse)
async def update_profile(
    data: ProfileUpdateRequest,
    auth: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
) -> UserProfileResponse:
    """Update current user's profile."""
    user = await session.get(User, auth.user.id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(user, field, value)

    await session.commit()
    await session.refresh(user)

    log.info("profile_updated", user_id=str(user.id), fields=list(update_data.keys()))

    return UserProfileResponse(
        id=user.id,
        email=user.email,
        name=user.name,
        bio=user.bio,
        timezone=user.timezone,
        avatar_url=user.avatar_url,
        email_verified_at=user.email_verified_at,
        created_at=user.created_at,
    )


# ============================================================================
# Preferences Endpoints
# ============================================================================


@router.get("/me/preferences", response_model=PreferencesResponse)
async def get_preferences(
    auth: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
) -> PreferencesResponse:
    """Get current user's preferences."""
    user = await session.get(User, auth.user.id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return PreferencesResponse(preferences=user.preferences or {})


@router.patch("/me/preferences", response_model=PreferencesResponse)
async def update_preferences(
    data: PreferencesUpdateRequest,
    auth: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
) -> PreferencesResponse:
    """Update current user's preferences (merge)."""
    user = await session.get(User, auth.user.id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    current = user.preferences or {}
    current.update(data.preferences)
    user.preferences = current

    await session.commit()
    await session.refresh(user)

    log.info("preferences_updated", user_id=str(user.id))

    return PreferencesResponse(preferences=user.preferences or {})


# ============================================================================
# Password Endpoints
# ============================================================================


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    data: PasswordChangeRequest,
    auth: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
) -> None:
    """Change current user's password."""
    user = await session.get(User, auth.user.id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not user.password_hash or not user.password_salt:
        raise HTTPException(
            status_code=400,
            detail="No password set. Use OAuth or set a password first.",
        )

    if not verify_password(
        data.current_password,
        salt_hex=user.password_salt,
        hash_hex=user.password_hash,
        iterations=user.password_iterations or 310_000,
    ):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    pw = hash_password(data.new_password)
    user.password_salt = pw.salt_hex
    user.password_hash = pw.hash_hex
    user.password_iterations = pw.iterations

    await session.commit()
    log.info("password_changed", user_id=str(user.id))


@router.post("/password/reset", status_code=status.HTTP_202_ACCEPTED)
async def request_password_reset(
    data: PasswordResetRequest,
    session: AsyncSession = Depends(get_session_dependency),
) -> dict[str, str]:
    """Request a password reset email."""
    from sibyl.auth.password_reset import PasswordResetManager
    from sibyl.email import get_email_client

    manager = PasswordResetManager(session, get_email_client())
    await manager.request_reset(data.email)

    return {"message": "If an account exists, a reset email has been sent."}


@router.post("/password/reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_password_reset(
    data: PasswordResetConfirmRequest,
    session: AsyncSession = Depends(get_session_dependency),
) -> None:
    """Confirm password reset with token."""
    from sibyl.auth.password_reset import PasswordResetError, PasswordResetManager
    from sibyl.email import get_email_client

    manager = PasswordResetManager(session, get_email_client())
    try:
        await manager.confirm_reset(data.token, data.new_password)
    except PasswordResetError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ============================================================================
# Session Endpoints
# ============================================================================


@router.get("/me/sessions", response_model=list[SessionResponse])
async def list_sessions(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
) -> list[SessionResponse]:
    """List current user's active sessions."""
    manager = SessionManager(session)
    sessions = await manager.list_user_sessions(auth.user.id)

    current_token_hash = None
    token = select_access_token(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get("sibyl_access_token"),
    )
    if token:
        import hashlib

        current_token_hash = hashlib.sha256(token.encode()).hexdigest()

    return [
        SessionResponse(
            id=s.id,
            user_agent=s.user_agent,
            ip_address=s.ip_address,
            created_at=s.created_at,
            expires_at=s.expires_at,
            last_used_at=s.last_active_at,
            is_current=s.token_hash == current_token_hash,
        )
        for s in sessions
    ]


@router.delete("/me/sessions", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_all_sessions(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
) -> None:
    """Revoke all sessions except current."""
    current_token_hash = None
    token = select_access_token(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get("sibyl_access_token"),
    )
    if token:
        import hashlib

        current_token_hash = hashlib.sha256(token.encode()).hexdigest()

    manager = SessionManager(session)
    count = await manager.revoke_all_sessions(auth.user.id, exclude_token_hash=current_token_hash)
    log.info("sessions_revoked", user_id=str(auth.user.id), count=count)


@router.delete("/me/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
) -> None:
    """Revoke a specific session."""
    manager = SessionManager(session)
    revoked = await manager.revoke_session(session_id, auth.user.id)
    if not revoked:
        raise HTTPException(status_code=404, detail="Session not found")


# ============================================================================
# OAuth Connections
# ============================================================================


@router.get("/me/connections", response_model=list[OAuthConnectionResponse])
async def list_connections(
    auth: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
) -> list[OAuthConnectionResponse]:
    """List OAuth connections for current user."""
    from sqlmodel import select

    result = await session.execute(
        select(OAuthConnection).where(OAuthConnection.user_id == auth.user.id)
    )
    connections = result.scalars().all()

    return [
        OAuthConnectionResponse(
            id=c.id,
            provider=c.provider,
            provider_user_id=c.provider_user_id,
            provider_email=c.provider_email,
            connected_at=c.created_at,
        )
        for c in connections
    ]


@router.delete("/me/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_connection(
    connection_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session_dependency),
) -> None:
    """Remove an OAuth connection."""
    from sqlmodel import select

    result = await session.execute(
        select(OAuthConnection)
        .where(OAuthConnection.id == connection_id)
        .where(OAuthConnection.user_id == auth.user.id)
    )
    connection = result.scalar_one_or_none()

    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")

    user = await session.get(User, auth.user.id)

    remaining = await session.execute(
        select(OAuthConnection)
        .where(OAuthConnection.user_id == auth.user.id)
        .where(OAuthConnection.id != connection_id)
    )
    has_other_connections = remaining.scalar_one_or_none() is not None
    has_password = user and user.password_hash

    if not has_other_connections and not has_password:
        raise HTTPException(
            status_code=400,
            detail="Cannot remove last login method. Set a password first.",
        )

    await session.delete(connection)
    await session.commit()

    log.info(
        "oauth_connection_removed",
        user_id=str(auth.user.id),
        provider=connection.provider,
    )
