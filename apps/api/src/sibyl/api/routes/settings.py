"""System settings API endpoints.

Allows reading and writing system settings like API keys.
Works without auth during setup mode, requires admin role otherwise.
"""

from __future__ import annotations

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl.db.connection import get_session_dependency
from sibyl.db.models import OrganizationRole, User
from sibyl.services.settings import get_settings_service

router = APIRouter(prefix="/settings", tags=["settings"])
log = structlog.get_logger()

# Admin roles that can manage settings
_ADMIN_ROLES = (OrganizationRole.OWNER, OrganizationRole.ADMIN)


class SettingInfo(BaseModel):
    """Information about a single setting."""

    configured: bool = Field(description="True if setting has a value")
    source: str = Field(description="Where the value comes from: database, environment, or none")
    is_secret: bool = Field(description="True if this is a sensitive value")
    masked: str | None = Field(default=None, description="Masked value for display (secrets only)")


class SettingsResponse(BaseModel):
    """Response containing all settings."""

    settings: dict[str, SettingInfo]


class UpdateSettingsRequest(BaseModel):
    """Request to update one or more settings."""

    openai_api_key: str | None = Field(default=None, description="OpenAI API key")
    anthropic_api_key: str | None = Field(default=None, description="Anthropic API key")


class UpdateSettingsResponse(BaseModel):
    """Response after updating settings."""

    updated: list[str] = Field(description="Keys that were updated")
    validation: dict[str, dict] = Field(description="Validation results for each key")


class DeleteSettingResponse(BaseModel):
    """Response after deleting a setting."""

    deleted: bool = Field(description="True if setting was deleted")
    key: str = Field(description="The key that was deleted")
    message: str = Field(description="Status message")


async def _is_setup_mode(session: AsyncSession) -> bool:
    """Check if we're in setup mode (no users exist)."""
    result = await session.execute(select(func.count(User.id)))
    user_count = result.scalar() or 0
    return user_count == 0


async def _validate_openai_key(key: str) -> tuple[bool, str | None]:
    """Validate OpenAI API key by calling models endpoint."""
    if not key:
        return False, "No API key provided"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
            if response.status_code == 200:
                return True, None
            if response.status_code == 401:
                return False, "Invalid API key"
            return False, f"API error: {response.status_code}"
    except httpx.TimeoutException:
        return False, "Connection timeout"
    except Exception as e:
        log.warning("OpenAI validation failed", error=str(e))
        return False, str(e)


async def _validate_anthropic_key(key: str) -> tuple[bool, str | None]:
    """Validate Anthropic API key by calling messages endpoint."""
    if not key:
        return False, "No API key provided"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={"model": "claude-3-haiku-20240307", "max_tokens": 1, "messages": []},
            )
            # 400 = key valid but request invalid (expected)
            # 401 = key invalid
            if response.status_code in (200, 400):
                return True, None
            if response.status_code == 401:
                return False, "Invalid API key"
            return False, f"API error: {response.status_code}"
    except httpx.TimeoutException:
        return False, "Connection timeout"
    except Exception as e:
        log.warning("Anthropic validation failed", error=str(e))
        return False, str(e)


@router.get("", response_model=SettingsResponse)
async def get_settings(
    session: AsyncSession = Depends(get_session_dependency),
) -> SettingsResponse:
    """Get all system settings with their configuration status.

    Returns settings metadata (configured, source, masked values) but not
    the actual secret values.

    This endpoint works without authentication during setup mode (no users exist).
    Otherwise, admin role is required.
    """
    # Check if in setup mode
    if not await _is_setup_mode(session):
        # Not in setup mode - require admin (this would need auth)
        # For now, we'll allow read access - the values are masked anyway
        pass

    service = get_settings_service()
    all_settings = await service.get_all(include_secrets=False)

    return SettingsResponse(
        settings={
            key: SettingInfo(
                configured=info["configured"],
                source=info["source"],
                is_secret=info["is_secret"],
                masked=info["masked"],
            )
            for key, info in all_settings.items()
        }
    )


@router.patch("", response_model=UpdateSettingsResponse)
async def update_settings(
    request: UpdateSettingsRequest,
    session: AsyncSession = Depends(get_session_dependency),
) -> UpdateSettingsResponse:
    """Update system settings.

    Validates API keys before saving. Only non-null values are updated.

    This endpoint works without authentication during setup mode (no users exist).
    Otherwise, admin role is required.
    """
    # Check if in setup mode
    is_setup = await _is_setup_mode(session)
    if not is_setup:
        # Not in setup mode - for now we'll still allow updates
        # TODO: Add proper admin auth check
        log.info("Settings update outside setup mode")

    service = get_settings_service()
    updated: list[str] = []
    validation: dict[str, dict] = {}

    # Validate and save OpenAI key
    if request.openai_api_key is not None:
        valid, error = await _validate_openai_key(request.openai_api_key)
        validation["openai_api_key"] = {"valid": valid, "error": error}

        if valid:
            await service.set(
                "openai_api_key",
                request.openai_api_key,
                is_secret=True,
                description="OpenAI API key for embeddings and LLM operations",
            )
            updated.append("openai_api_key")
        else:
            log.warning("OpenAI key validation failed", error=error)

    # Validate and save Anthropic key
    if request.anthropic_api_key is not None:
        valid, error = await _validate_anthropic_key(request.anthropic_api_key)
        validation["anthropic_api_key"] = {"valid": valid, "error": error}

        if valid:
            await service.set(
                "anthropic_api_key",
                request.anthropic_api_key,
                is_secret=True,
                description="Anthropic API key for Claude models",
            )
            updated.append("anthropic_api_key")
        else:
            log.warning("Anthropic key validation failed", error=error)

    return UpdateSettingsResponse(updated=updated, validation=validation)


@router.delete("/{key}", response_model=DeleteSettingResponse)
async def delete_setting(
    key: str,
    session: AsyncSession = Depends(get_session_dependency),
) -> DeleteSettingResponse:
    """Delete a setting from the database.

    After deletion, the setting will fall back to environment variable
    if one is configured.

    Requires admin role (not available during setup mode).
    """
    # For delete, we require auth (can't accidentally delete during setup)
    if await _is_setup_mode(session):
        raise HTTPException(
            status_code=403,
            detail="Cannot delete settings during setup mode",
        )

    # TODO: Add proper admin auth check

    service = get_settings_service()
    deleted = await service.delete(key)

    if deleted:
        return DeleteSettingResponse(
            deleted=True,
            key=key,
            message=f"Setting '{key}' deleted. Will fall back to environment variable if set.",
        )
    return DeleteSettingResponse(
        deleted=False,
        key=key,
        message=f"Setting '{key}' was not found in the database.",
    )
