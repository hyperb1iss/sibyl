"""User profile and settings API routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context
from sibyl.auth.http import select_access_token
from sibyl.persistence.auth_runtime import (
    confirm_password_reset as confirm_password_reset_token,
    list_oauth_connections,
    list_user_sessions,
    patch_auth_user,
    remove_oauth_connection,
    request_password_reset as request_password_reset_token,
    revoke_all_user_sessions,
    revoke_user_session,
    update_auth_user,
)

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
    avatar_url: str | None = Field(
        None, max_length=7_000_000
    )  # Supports data URLs up to ~5MB images


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
) -> UserProfileResponse:
    """Get current user's profile."""
    user = auth.user

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
) -> UserProfileResponse:
    """Update current user's profile."""
    update_data = data.model_dump(exclude_unset=True)
    user = await patch_auth_user(
        user_id=auth.user.id,
        updates=update_data,
        organization_id=auth.organization.id if auth.organization else None,
        request=None,
    )

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
) -> PreferencesResponse:
    """Get current user's preferences."""
    return PreferencesResponse(preferences=auth.user.preferences or {})


@router.patch("/me/preferences", response_model=PreferencesResponse)
async def update_preferences(
    data: PreferencesUpdateRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> PreferencesResponse:
    """Update current user's preferences (merge)."""
    current = dict(auth.user.preferences or {})
    current.update(data.preferences)
    user = await patch_auth_user(
        user_id=auth.user.id,
        updates={"preferences": current},
        organization_id=auth.organization.id if auth.organization else None,
        request=None,
    )

    log.info("preferences_updated", user_id=str(user.id))

    return PreferencesResponse(preferences=user.preferences or {})


# ============================================================================
# Password Endpoints
# ============================================================================


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    data: PasswordChangeRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> None:
    """Change current user's password."""
    user = await update_auth_user(
        user_id=auth.user.id,
        email=None,
        name=None,
        avatar_url=None,
        current_password=data.current_password,
        new_password=data.new_password,
        organization_id=auth.organization.id if auth.organization else None,
        request=None,
    )
    log.info("password_changed", user_id=str(user.id))


@router.post("/password/reset", status_code=status.HTTP_202_ACCEPTED)
async def request_password_reset(
    data: PasswordResetRequest,
) -> dict[str, str]:
    """Request a password reset email."""
    await request_password_reset_token(data.email)

    return {"message": "If an account exists, a reset email has been sent."}


@router.post("/password/reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_password_reset(
    data: PasswordResetConfirmRequest,
) -> None:
    """Confirm password reset with token."""
    await confirm_password_reset_token(data.token, data.new_password)


# ============================================================================
# Session Endpoints
# ============================================================================


@router.get("/me/sessions", response_model=list[SessionResponse])
async def list_sessions(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> list[SessionResponse]:
    """List current user's active sessions."""
    sessions = await list_user_sessions(user_id=auth.user.id)

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

    count = await revoke_all_user_sessions(
        user_id=auth.user.id,
        exclude_token_hash=current_token_hash,
    )
    log.info("sessions_revoked", user_id=str(auth.user.id), count=count)


@router.delete("/me/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
) -> None:
    """Revoke a specific session."""
    revoked = await revoke_user_session(
        session_id=session_id,
        user_id=auth.user.id,
    )
    if not revoked:
        raise HTTPException(status_code=404, detail="Session not found")


# ============================================================================
# OAuth Connections
# ============================================================================


@router.get("/me/connections", response_model=list[OAuthConnectionResponse])
async def list_connections(
    auth: AuthContext = Depends(get_auth_context),
) -> list[OAuthConnectionResponse]:
    """List OAuth connections for current user."""
    connections = await list_oauth_connections(user_id=auth.user.id)

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
) -> None:
    """Remove an OAuth connection."""
    connection = await remove_oauth_connection(
        user_id=auth.user.id,
        connection_id=connection_id,
    )

    log.info(
        "oauth_connection_removed",
        user_id=str(auth.user.id),
        provider=connection.provider,
    )
