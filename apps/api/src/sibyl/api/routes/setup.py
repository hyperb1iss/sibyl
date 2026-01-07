"""Setup wizard endpoints.

Public endpoints for detecting fresh installs and guiding first-time setup.
Status endpoint is always public. Other endpoints require authentication
once initial setup is complete (users exist).

Config update endpoints are admin-only after initial setup.
"""

from __future__ import annotations

from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import JwtError, verify_access_token
from sibyl.config import settings
from sibyl.db.connection import get_session_dependency
from sibyl.db.models import Organization, User
from sibyl.services.settings import get_settings_service

router = APIRouter(prefix="/setup", tags=["setup"])
log = structlog.get_logger()


async def _is_setup_complete(session: AsyncSession) -> bool:
    """Check if initial setup is complete (users exist)."""
    result = await session.execute(select(func.count(User.id)))
    return (result.scalar() or 0) > 0


async def require_setup_mode_or_auth(
    request: Request,
    session: AsyncSession = Depends(get_session_dependency),
) -> None:
    """Gate endpoint: allow if in setup mode (no users) OR authenticated.

    This protects sensitive setup endpoints after initial setup is complete.
    During setup (no users exist), allows unrestricted access.
    After setup, requires valid authentication.

    Raises:
        HTTPException 401: If setup is complete and not authenticated
    """
    if not await _is_setup_complete(session):
        # Setup mode - allow access
        return

    # Setup complete - require authentication
    token = select_access_token(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get("sibyl_access_token"),
    )

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Setup is complete. Authentication required.",
        )

    try:
        verify_access_token(token)
    except JwtError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
        ) from e


async def require_setup_mode_or_admin(
    request: Request,
    session: AsyncSession = Depends(get_session_dependency),
) -> User | None:
    """Gate endpoint: allow if in setup mode (no users) OR user is admin.

    This protects config update endpoints that should only be accessible to:
    - Anyone during initial setup (no users exist)
    - System admins after setup is complete

    Returns:
        User if authenticated, None if in setup mode

    Raises:
        HTTPException 401: If setup is complete and not authenticated
        HTTPException 403: If setup is complete and user is not an admin
    """
    if not await _is_setup_complete(session):
        # Setup mode - allow access
        return None

    # Setup complete - require admin
    token = select_access_token(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get("sibyl_access_token"),
    )

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Setup is complete. Authentication required.",
        )

    try:
        claims = verify_access_token(token)
    except JwtError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
        ) from e

    # Get user from token
    try:
        user_id = UUID(str(claims.get("sub", "")))
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: missing user ID",
        ) from e

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


class SetupStatus(BaseModel):
    """Current setup state of the Sibyl instance."""

    needs_setup: bool = Field(description="True if no users exist yet")
    has_users: bool = Field(description="True if at least one user exists")
    has_orgs: bool = Field(description="True if at least one org exists")
    openai_configured: bool = Field(description="True if OpenAI API key is set")
    anthropic_configured: bool = Field(description="True if Anthropic API key is set")
    openai_valid: bool | None = Field(
        default=None, description="True if OpenAI key works (only checked if configured)"
    )
    anthropic_valid: bool | None = Field(
        default=None, description="True if Anthropic key works (only checked if configured)"
    )


class ApiKeyValidation(BaseModel):
    """Result of validating API keys."""

    openai_valid: bool = Field(description="True if OpenAI API key works")
    anthropic_valid: bool = Field(description="True if Anthropic API key works")
    openai_error: str | None = Field(default=None, description="Error message if OpenAI fails")
    anthropic_error: str | None = Field(
        default=None, description="Error message if Anthropic fails"
    )


async def _check_openai_key(key: str | None = None) -> tuple[bool, str | None]:
    """Validate OpenAI API key by calling models endpoint.

    Args:
        key: API key to validate. If None, fetches from SettingsService.
    """
    if key is None:
        service = get_settings_service()
        key = await service.get_openai_key()

    if not key:
        return False, "No API key configured"

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


async def _check_anthropic_key(key: str | None = None) -> tuple[bool, str | None]:
    """Validate Anthropic API key by calling messages endpoint with minimal request.

    Args:
        key: API key to validate. If None, fetches from SettingsService.
    """
    if key is None:
        service = get_settings_service()
        key = await service.get_anthropic_key()

    if not key:
        return False, "No API key configured"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Use a minimal request to validate the key
            # We intentionally use an invalid request to avoid charges
            # A 400 with "invalid_request_error" means the key is valid
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={"model": "claude-3-haiku-20240307", "max_tokens": 1, "messages": []},
            )
            # 400 = key valid but request invalid (expected - we sent empty messages)
            # 401 = key invalid
            # 200 = somehow worked (shouldn't happen with empty messages)
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


@router.get("/status", response_model=SetupStatus)
async def get_setup_status(
    session: AsyncSession = Depends(get_session_dependency),
    validate_keys: bool = False,
) -> SetupStatus:
    """Check if this Sibyl instance needs initial setup.

    Returns the current setup state including:
    - Whether any users exist (needs_setup = no users)
    - Whether API keys are configured
    - Optionally validates API keys work (validate_keys=true)

    This endpoint requires no authentication since it must work
    before any users exist.
    """
    # Count users
    user_result = await session.execute(select(func.count(User.id)))
    user_count = user_result.scalar() or 0

    # Count orgs
    org_result = await session.execute(select(func.count(Organization.id)))
    org_count = org_result.scalar() or 0

    # Check if API keys are configured (non-empty)
    service = get_settings_service()
    openai_key = await service.get_openai_key()
    anthropic_key = await service.get_anthropic_key()
    openai_configured = bool(openai_key)
    anthropic_configured = bool(anthropic_key)

    # Optionally validate keys work
    openai_valid: bool | None = None
    anthropic_valid: bool | None = None

    if validate_keys:
        if openai_configured:
            openai_valid, _ = await _check_openai_key(openai_key)
        if anthropic_configured:
            anthropic_valid, _ = await _check_anthropic_key(anthropic_key)

    return SetupStatus(
        needs_setup=user_count == 0,
        has_users=user_count > 0,
        has_orgs=org_count > 0,
        openai_configured=openai_configured,
        anthropic_configured=anthropic_configured,
        openai_valid=openai_valid,
        anthropic_valid=anthropic_valid,
    )


@router.get(
    "/validate-keys",
    response_model=ApiKeyValidation,
    dependencies=[Depends(require_setup_mode_or_auth)],
)
async def validate_api_keys() -> ApiKeyValidation:
    """Validate that configured API keys work.

    Makes test requests to OpenAI and Anthropic APIs to verify
    the configured keys are valid and have appropriate permissions.

    During initial setup (no users): accessible without auth.
    After setup: requires authentication.
    """
    openai_valid, openai_error = await _check_openai_key()
    anthropic_valid, anthropic_error = await _check_anthropic_key()

    return ApiKeyValidation(
        openai_valid=openai_valid,
        anthropic_valid=anthropic_valid,
        openai_error=openai_error,
        anthropic_error=anthropic_error,
    )


@router.get("/mcp-command", dependencies=[Depends(require_setup_mode_or_auth)])
async def get_mcp_command() -> dict[str, str]:
    """Get the Claude Code command to connect to this Sibyl instance.

    Returns the command users should run to add this Sibyl server
    to their Claude Code configuration.

    During initial setup (no users): accessible without auth.
    After setup: requires authentication.
    """
    # Use the configured server URL or fall back to localhost
    server_url = settings.server_url.rstrip("/")

    return {
        "command": f"claude mcp add sibyl --transport http {server_url}/mcp",
        "server_url": f"{server_url}/mcp",
        "description": "Run this command in your terminal to connect Claude Code to Sibyl",
    }


class ConfigUpdateRequest(BaseModel):
    """Request to update server configuration."""

    openai_api_key: str | None = Field(
        default=None, description="OpenAI API key (leave empty to keep existing)"
    )
    anthropic_api_key: str | None = Field(
        default=None, description="Anthropic API key (leave empty to keep existing)"
    )


class ConfigUpdateResponse(BaseModel):
    """Response after updating server configuration."""

    success: bool = Field(description="True if config was updated")
    openai_configured: bool = Field(description="True if OpenAI key is now configured")
    anthropic_configured: bool = Field(description="True if Anthropic key is now configured")
    openai_valid: bool | None = Field(
        default=None, description="True if OpenAI key works (validated if provided)"
    )
    anthropic_valid: bool | None = Field(
        default=None, description="True if Anthropic key works (validated if provided)"
    )
    openai_error: str | None = Field(default=None, description="Error if OpenAI validation failed")
    anthropic_error: str | None = Field(
        default=None, description="Error if Anthropic validation failed"
    )


@router.post("/config", response_model=ConfigUpdateResponse)
async def update_config(
    body: ConfigUpdateRequest,
    _admin: User | None = Depends(require_setup_mode_or_admin),
) -> ConfigUpdateResponse:
    """Update server configuration (API keys).

    During initial setup (no users): accessible without auth.
    After setup: requires admin authentication.

    Keys are validated before being saved. If validation fails, the key
    is still saved but the response indicates the error.
    """
    service = get_settings_service()

    openai_valid: bool | None = None
    anthropic_valid: bool | None = None
    openai_error: str | None = None
    anthropic_error: str | None = None

    # Update OpenAI key if provided
    if body.openai_api_key is not None:
        openai_valid, openai_error = await _check_openai_key(body.openai_api_key)
        await service.set(
            "openai_api_key",
            body.openai_api_key,
            is_secret=True,
            description="OpenAI API key for embeddings and entity extraction",
        )
        log.info("OpenAI API key updated", valid=openai_valid)

    # Update Anthropic key if provided
    if body.anthropic_api_key is not None:
        anthropic_valid, anthropic_error = await _check_anthropic_key(body.anthropic_api_key)
        await service.set(
            "anthropic_api_key",
            body.anthropic_api_key,
            is_secret=True,
            description="Anthropic API key for Claude agents",
        )
        log.info("Anthropic API key updated", valid=anthropic_valid)

    # Get current config state
    openai_key = await service.get_openai_key()
    anthropic_key = await service.get_anthropic_key()

    return ConfigUpdateResponse(
        success=True,
        openai_configured=bool(openai_key),
        anthropic_configured=bool(anthropic_key),
        openai_valid=openai_valid,
        anthropic_valid=anthropic_valid,
        openai_error=openai_error,
        anthropic_error=anthropic_error,
    )


class ConfigStatusResponse(BaseModel):
    """Current server configuration status."""

    openai_configured: bool = Field(description="True if OpenAI key is configured")
    anthropic_configured: bool = Field(description="True if Anthropic key is configured")
    openai_source: str = Field(description="Source of OpenAI key: database, environment, or none")
    anthropic_source: str = Field(
        description="Source of Anthropic key: database, environment, or none"
    )


@router.get("/config", response_model=ConfigStatusResponse)
async def get_config_status(
    _admin: User | None = Depends(require_setup_mode_or_admin),
) -> ConfigStatusResponse:
    """Get current server configuration status.

    During initial setup (no users): accessible without auth.
    After setup: requires admin authentication.

    Returns whether each API key is configured and its source (database or environment).
    Does not return the actual key values for security.
    """
    service = get_settings_service()

    openai_value, openai_source = await service.get_with_source("openai_api_key")
    anthropic_value, anthropic_source = await service.get_with_source("anthropic_api_key")

    return ConfigStatusResponse(
        openai_configured=bool(openai_value),
        anthropic_configured=bool(anthropic_value),
        openai_source=openai_source,
        anthropic_source=anthropic_source,
    )
