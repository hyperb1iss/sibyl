"""System settings API endpoints.

Allows reading and writing system settings like API keys.
Works without auth during setup mode, requires admin role otherwise.
"""

from __future__ import annotations

import os

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from sibyl.persistence.operations_runtime import (
    is_setup_mode,
    require_settings_admin,
)
from sibyl.services.settings import get_settings_service

router = APIRouter(prefix="/settings", tags=["settings"])
log = structlog.get_logger()


async def reset_graph_runtime() -> None:
    from sibyl.persistence.graph_runtime import reset_graph_runtime as service

    await service()


async def _try_reset_graph_client(context: str) -> None:
    """Reset the global GraphClient, logging on failure.

    Args:
        context: Description for log message (e.g., "API key update", "API key deletion")
    """
    try:
        await reset_graph_runtime()
        log.info(f"Reset GraphClient after {context}")
    except Exception as e:
        log.warning("Failed to reset GraphClient", error=str(e))


class SettingInfo(BaseModel):
    """Information about a single setting."""

    configured: bool = Field(description="True if setting has a value")
    source: str = Field(description="Where the value comes from: database, environment, or none")
    is_secret: bool = Field(description="True if this is a sensitive value")
    masked: str | None = Field(default=None, description="Masked value for display (secrets only)")
    value: str | None = Field(default=None, description="Plain value for non-secret settings")


class SettingsResponse(BaseModel):
    """Response containing all settings."""

    settings: dict[str, SettingInfo]


class UpdateSettingsRequest(BaseModel):
    """Request to update one or more settings."""

    openai_api_key: str | None = Field(default=None, description="OpenAI API key")
    anthropic_api_key: str | None = Field(default=None, description="Anthropic API key")
    gemini_api_key: str | None = Field(default=None, description="Gemini API key")
    embedding_provider: str | None = Field(
        default=None, pattern="^(openai|gemini)$", description="Document embedding provider"
    )
    embedding_model: str | None = Field(default=None, description="Document embedding model")
    embedding_dimensions: int | None = Field(
        default=None, ge=128, le=3072, description="Document embedding dimensions"
    )
    graph_embedding_provider: str | None = Field(
        default=None, pattern="^(openai|gemini)$", description="Graph embedding provider"
    )
    graph_embedding_model: str | None = Field(default=None, description="Graph embedding model")
    graph_embedding_dimensions: int | None = Field(
        default=None, ge=128, le=3072, description="Graph embedding dimensions"
    )


class UpdateSettingsResponse(BaseModel):
    """Response after updating settings."""

    updated: list[str] = Field(description="Keys that were updated")
    validation: dict[str, dict] = Field(description="Validation results for each key")


class DeleteSettingResponse(BaseModel):
    """Response after deleting a setting."""

    deleted: bool = Field(description="True if setting was deleted")
    key: str = Field(description="The key that was deleted")
    message: str = Field(description="Status message")


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


async def _validate_gemini_key(key: str) -> tuple[bool, str | None]:
    """Validate Gemini API key by calling the embeddings endpoint."""
    if not key:
        return False, "No API key provided"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://generativelanguage.googleapis.com/v1beta/"
                "models/gemini-embedding-2:embedContent",
                headers={
                    "x-goog-api-key": key,
                    "content-type": "application/json",
                },
                json={
                    "content": {"parts": [{"text": "sibyl api key validation"}]},
                    "outputDimensionality": 128,
                },
            )
            if response.status_code == 200:
                return True, None
            if response.status_code in (400, 401, 403):
                return False, "Invalid API key"
            return False, f"API error: {response.status_code}"
    except httpx.TimeoutException:
        return False, "Connection timeout"
    except Exception as e:
        log.warning("Gemini validation failed", error=str(e))
        return False, str(e)


_SETTING_ENV_WRITES: dict[str, tuple[str, ...]] = {
    "openai_api_key": ("OPENAI_API_KEY",),
    "anthropic_api_key": ("ANTHROPIC_API_KEY",),
    "gemini_api_key": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "embedding_provider": ("SIBYL_EMBEDDING_PROVIDER",),
    "embedding_model": ("SIBYL_EMBEDDING_MODEL",),
    "embedding_dimensions": ("SIBYL_EMBEDDING_DIMENSIONS",),
    "graph_embedding_provider": ("SIBYL_GRAPH_EMBEDDING_PROVIDER",),
    "graph_embedding_model": ("SIBYL_GRAPH_EMBEDDING_MODEL",),
    "graph_embedding_dimensions": ("SIBYL_GRAPH_EMBEDDING_DIMENSIONS",),
}

_SETTING_DESCRIPTIONS = {
    "openai_api_key": "OpenAI API key for embeddings and LLM operations",
    "anthropic_api_key": "Anthropic API key for Claude models",
    "gemini_api_key": "Gemini API key for Google embeddings",
    "embedding_provider": "Document chunk embedding provider",
    "embedding_model": "Document chunk embedding model",
    "embedding_dimensions": "Document chunk embedding dimensions",
    "graph_embedding_provider": "Graph embedding provider",
    "graph_embedding_model": "Graph embedding model",
    "graph_embedding_dimensions": "Graph embedding dimensions",
}


def _write_runtime_env(key: str, value: object) -> None:
    for env_var in _SETTING_ENV_WRITES.get(key, ()):
        os.environ[env_var] = str(value)


@router.get("", response_model=SettingsResponse)
async def get_settings(
    request: Request,
) -> SettingsResponse:
    """Get all system settings with their configuration status.

    Returns settings metadata (configured, source, masked values) but not
    the actual secret values.

    This endpoint works without authentication during setup mode (no users exist).
    Otherwise, admin role is required.
    """
    await require_settings_admin(request)

    service = get_settings_service()
    all_settings = await service.get_all(include_secrets=False)

    return SettingsResponse(
        settings={
            key: SettingInfo(
                configured=info["configured"],
                source=info["source"],
                is_secret=info["is_secret"],
                masked=info["masked"],
                value=info.get("value"),
            )
            for key, info in all_settings.items()
        }
    )


@router.patch("", response_model=UpdateSettingsResponse)
async def update_settings(
    request: Request,
    body: UpdateSettingsRequest,
) -> UpdateSettingsResponse:
    """Update system settings.

    Validates API keys before saving. Only non-null values are updated.

    This endpoint works without authentication during setup mode (no users exist).
    Otherwise, admin role is required.
    """
    await require_settings_admin(request)

    service = get_settings_service()
    updated: list[str] = []
    validation: dict[str, dict] = {}

    # Validate and save OpenAI key
    if body.openai_api_key is not None:
        valid, error = await _validate_openai_key(body.openai_api_key)
        validation["openai_api_key"] = {"valid": valid, "error": error}

        if valid:
            await service.set(
                "openai_api_key",
                body.openai_api_key,
                is_secret=True,
                description="OpenAI API key for embeddings and LLM operations",
            )
            updated.append("openai_api_key")
            # Update environment variable so running server uses new key immediately
            # This bridges webapp settings to GraphClient which reads from env vars
            os.environ["OPENAI_API_KEY"] = body.openai_api_key
            log.info("Updated OpenAI API key in environment")
        else:
            log.warning("OpenAI key validation failed", error=error)

    # Validate and save Anthropic key
    if body.anthropic_api_key is not None:
        valid, error = await _validate_anthropic_key(body.anthropic_api_key)
        validation["anthropic_api_key"] = {"valid": valid, "error": error}

        if valid:
            await service.set(
                "anthropic_api_key",
                body.anthropic_api_key,
                is_secret=True,
                description="Anthropic API key for Claude models",
            )
            updated.append("anthropic_api_key")
            # Update environment variable so running server uses new key immediately
            os.environ["ANTHROPIC_API_KEY"] = body.anthropic_api_key
            log.info("Updated Anthropic API key in environment")
        else:
            log.warning("Anthropic key validation failed", error=error)

    # Validate and save Gemini key
    if body.gemini_api_key is not None:
        valid, error = await _validate_gemini_key(body.gemini_api_key)
        validation["gemini_api_key"] = {"valid": valid, "error": error}

        if valid:
            await service.set(
                "gemini_api_key",
                body.gemini_api_key,
                is_secret=True,
                description=_SETTING_DESCRIPTIONS["gemini_api_key"],
            )
            updated.append("gemini_api_key")
            _write_runtime_env("gemini_api_key", body.gemini_api_key)
            log.info("Updated Gemini API key in environment")
        else:
            log.warning("Gemini key validation failed", error=error)

    for key in (
        "embedding_provider",
        "embedding_model",
        "embedding_dimensions",
        "graph_embedding_provider",
        "graph_embedding_model",
        "graph_embedding_dimensions",
    ):
        value = getattr(body, key)
        if value is None:
            continue
        await service.set(
            key,
            str(value),
            is_secret=False,
            description=_SETTING_DESCRIPTIONS[key],
        )
        updated.append(key)
        _write_runtime_env(key, value)

    # If API keys or graph embedding settings were updated, reset the GraphClient
    # so it reconnects with fresh provider/model/key configuration.
    if updated:
        await _try_reset_graph_client(f"API key update keys={updated}")

    return UpdateSettingsResponse(updated=updated, validation=validation)


@router.delete("/{key}", response_model=DeleteSettingResponse)
async def delete_setting(
    request: Request,
    key: str,
) -> DeleteSettingResponse:
    """Delete a setting from the database.

    After deletion, the setting will fall back to environment variable
    if one is configured.

    Requires admin role (not available during setup mode).
    """
    if await is_setup_mode():
        raise HTTPException(status_code=403, detail="Cannot delete settings during setup mode")

    await require_settings_admin(request)

    service = get_settings_service()
    deleted = await service.delete(key)

    if deleted:
        # Clear from environment and reset GraphClient if this was an API key
        if key in _SETTING_ENV_WRITES:
            # Note: This clears the env var even if it was externally set. Since webapp users
            # typically configure keys via UI (not external env), this is the expected behavior.
            # If external env vars need to be preserved, track DB-loaded keys at startup.
            for env_key in _SETTING_ENV_WRITES[key]:
                os.environ.pop(env_key, None)
            await _try_reset_graph_client(f"API key deletion key={key}")

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
