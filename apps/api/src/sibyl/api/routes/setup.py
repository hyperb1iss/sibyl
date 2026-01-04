"""Setup wizard endpoints.

Public endpoints for detecting fresh installs and guiding first-time setup.
No authentication required - these run before any users exist.
"""

from __future__ import annotations

import httpx
import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl.config import settings
from sibyl.db.connection import get_session_dependency
from sibyl.db.models import Organization, User

router = APIRouter(prefix="/setup", tags=["setup"])
log = structlog.get_logger()


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


async def _check_openai_key() -> tuple[bool, str | None]:
    """Validate OpenAI API key by calling models endpoint."""
    if not settings.openai_api_key:
        return False, "No API key configured"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
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


async def _check_anthropic_key() -> tuple[bool, str | None]:
    """Validate Anthropic API key by calling messages endpoint with minimal request."""
    if not settings.anthropic_api_key:
        return False, "No API key configured"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Use a minimal request to validate the key
            # We intentionally use an invalid request to avoid charges
            # A 400 with "invalid_request_error" means the key is valid
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.anthropic_api_key,
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
    openai_configured = bool(settings.openai_api_key)
    anthropic_configured = bool(settings.anthropic_api_key)

    # Optionally validate keys work
    openai_valid: bool | None = None
    anthropic_valid: bool | None = None

    if validate_keys:
        if openai_configured:
            openai_valid, _ = await _check_openai_key()
        if anthropic_configured:
            anthropic_valid, _ = await _check_anthropic_key()

    return SetupStatus(
        needs_setup=user_count == 0,
        has_users=user_count > 0,
        has_orgs=org_count > 0,
        openai_configured=openai_configured,
        anthropic_configured=anthropic_configured,
        openai_valid=openai_valid,
        anthropic_valid=anthropic_valid,
    )


@router.get("/validate-keys", response_model=ApiKeyValidation)
async def validate_api_keys() -> ApiKeyValidation:
    """Validate that configured API keys work.

    Makes test requests to OpenAI and Anthropic APIs to verify
    the configured keys are valid and have appropriate permissions.

    This endpoint requires no authentication since it must work
    during initial setup.
    """
    openai_valid, openai_error = await _check_openai_key()
    anthropic_valid, anthropic_error = await _check_anthropic_key()

    return ApiKeyValidation(
        openai_valid=openai_valid,
        anthropic_valid=anthropic_valid,
        openai_error=openai_error,
        anthropic_error=anthropic_error,
    )


@router.get("/mcp-command")
async def get_mcp_command() -> dict[str, str]:
    """Get the Claude Code command to connect to this Sibyl instance.

    Returns the command users should run to add this Sibyl server
    to their Claude Code configuration.
    """
    # Use the configured server URL or fall back to localhost
    server_url = settings.server_url.rstrip("/")

    return {
        "command": f"claude mcp add sibyl --transport http {server_url}/mcp",
        "server_url": f"{server_url}/mcp",
        "description": "Run this command in your terminal to connect Claude Code to Sibyl",
    }
