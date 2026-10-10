"""System settings API endpoints.

Allows reading and writing system settings like API keys.
Works without auth during setup mode, requires global admin otherwise.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from sibyl.cache_invalidation import announce_runtime_settings_changed
from sibyl.crypto import mask_secret
from sibyl.persistence.operations_runtime import (
    is_setup_mode,
    require_settings_owner,
)
from sibyl.services.settings import (
    RUNTIME_SETTING_ENV_VARS,
    SECRET_SETTINGS,
    deployment_environment,
    get_settings_service,
    runtime_setting_lock,
    sync_runtime_settings,
)
from sibyl_core.ai.bedrock import COHERE_EMBED_V4_DIMENSIONS
from sibyl_core.ai.llm.config import LLMProviderName
from sibyl_core.ai.validation import KeyValidationResult, check_provider_key

router = APIRouter(prefix="/settings", tags=["settings"])
log = structlog.get_logger()


def _deployment_owned_settings() -> list[dict[str, str]]:
    return [
        {"field": key, "env_var": env_var}
        for key in RUNTIME_SETTING_ENV_VARS
        if (env_var := runtime_setting_lock(key)) is not None
    ]


def _unchanged_deployment_settings(requested: dict[str, object]) -> set[str]:
    """Deployment-owned settings submitted unchanged, which the update skips.

    A stored value for a setting the deployment owns would never take effect
    on this replica, and a replica started without that variable would apply
    it, so replicas would disagree. Resubmitting the deployment's own value
    changes nothing and is accepted; any other value is refused with 409, as
    the LLM settings routes do. The refusal names every setting the
    deployment owns here, so a client can show them read-only.
    """
    environment = deployment_environment()
    unchanged: set[str] = set()
    conflicts: list[dict[str, str]] = []
    for key, value in requested.items():
        env_var = runtime_setting_lock(key)
        if env_var is None:
            continue
        if str(value).strip() == environment[env_var].strip():
            unchanged.add(key)
        else:
            conflicts.append({"field": key, "env_var": env_var})
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "LOCKED_BY_ENV",
                "fields": conflicts,
                "deployment_owned": _deployment_owned_settings(),
            },
        )
    return unchanged


async def _apply_runtime_settings(keys: list[str]) -> None:
    """Apply stored runtime settings here, then on every other replica and worker."""
    runtime_keys = [key for key in keys if key in RUNTIME_SETTING_ENV_VARS]
    if not runtime_keys:
        return
    try:
        await sync_runtime_settings(reason=f"settings changed: {', '.join(runtime_keys)}")
    finally:
        await announce_runtime_settings_changed(runtime_keys)


class SettingInfo(BaseModel):
    """Information about a single setting."""

    configured: bool = Field(description="True if setting has a value")
    source: str = Field(description="Where the value comes from: database, environment, or none")
    is_secret: bool = Field(description="True if this is a sensitive value")
    masked: str | None = Field(default=None, description="Masked value for display (secrets only)")
    value: str | None = Field(default=None, description="Plain value for non-secret settings")
    locked_by_env: bool = Field(
        default=False,
        description="True when the deployment environment owns this setting; it is read-only here",
    )
    env_var: str | None = Field(
        default=None, description="The deployment variable that owns this setting, if any"
    )


class SettingsResponse(BaseModel):
    """Response containing all settings."""

    settings: dict[str, SettingInfo]


class UpdateSettingsRequest(BaseModel):
    """Request to update one or more settings."""

    openai_api_key: str | None = Field(default=None, description="OpenAI API key")
    anthropic_api_key: str | None = Field(default=None, description="Anthropic API key")
    gemini_api_key: str | None = Field(default=None, description="Gemini API key")
    embedding_provider: str | None = Field(
        default=None,
        pattern="^(openai|gemini|bedrock)$",
        description="Document embedding provider",
    )
    embedding_model: str | None = Field(default=None, description="Document embedding model")
    embedding_dimensions: int | None = Field(
        default=None, ge=128, le=3072, description="Document embedding dimensions"
    )
    graph_embedding_provider: str | None = Field(
        default=None,
        pattern="^(openai|gemini|local|bedrock)$",
        description="Graph embedding provider",
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
    """Validate OpenAI API key through the native LLM substrate."""
    return await _validate_provider_key("openai", key)


async def _validate_anthropic_key(key: str) -> tuple[bool, str | None]:
    """Validate Anthropic API key through the native LLM substrate."""
    return await _validate_provider_key("anthropic", key)


async def _validate_gemini_key(key: str) -> tuple[bool, str | None]:
    """Validate Gemini API key through the native LLM substrate."""
    return await _validate_provider_key("gemini", key)


async def _validate_provider_key(
    provider: LLMProviderName,
    key: str,
) -> tuple[bool, str | None]:
    if not key:
        return False, "No API key provided"

    try:
        result = await check_provider_key(provider, key)
    except Exception as e:
        log.warning("Provider key validation failed", provider=provider, error=str(e))
        return False, str(e)
    return result.valid, _validation_error(result)


def _validation_error(result: KeyValidationResult) -> str | None:
    if result.valid:
        return None
    return result.error or result.status


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


async def _reject_unservable_bedrock_dimensions(body: UpdateSettingsRequest) -> None:
    """Refuse a Bedrock plane at a size Cohere Embed v4 cannot produce.

    Saving it would make every embedding call on that plane fail until an admin
    notices, so the effective provider and size after this update are checked.
    """
    from sibyl.api.routes.setup import effective_embedding_setting

    for provider_key, dimensions_key in (
        ("embedding_provider", "embedding_dimensions"),
        ("graph_embedding_provider", "graph_embedding_dimensions"),
    ):
        if getattr(body, provider_key) is None and getattr(body, dimensions_key) is None:
            continue
        provider = getattr(body, provider_key) or await effective_embedding_setting(provider_key)
        if provider != "bedrock":
            continue
        dimensions = getattr(body, dimensions_key) or await effective_embedding_setting(
            dimensions_key
        )
        if dimensions is None or int(dimensions) not in COHERE_EMBED_V4_DIMENSIONS:
            supported = ", ".join(str(size) for size in COHERE_EMBED_V4_DIMENSIONS)
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{dimensions_key}={dimensions} cannot be served by Cohere Embed v4 on "
                    f"Bedrock; use {supported}"
                ),
            )


@router.get("", response_model=SettingsResponse)
async def get_settings(
    request: Request,
) -> SettingsResponse:
    """Get all system settings with their configuration status.

    Returns settings metadata (configured, source, masked values) but not
    the actual secret values.

    This endpoint works without authentication during setup mode (no users exist).
    Otherwise, global admin access is required.
    """
    await require_settings_owner(request)

    service = get_settings_service()
    all_settings = await service.get_all(include_secrets=False)

    settings = {
        key: SettingInfo(
            configured=info["configured"],
            source=info["source"],
            is_secret=info["is_secret"],
            masked=info["masked"],
            value=info.get("value"),
        )
        for key, info in all_settings.items()
    }
    # What is in effect: a setting the deployment owns reports the
    # deployment's value, whatever is stored.
    environment = deployment_environment()
    for owned in _deployment_owned_settings():
        key, env_var = owned["field"], owned["env_var"]
        value = environment[env_var]
        is_secret = key in SECRET_SETTINGS
        settings[key] = SettingInfo(
            configured=True,
            source="environment",
            is_secret=is_secret,
            masked=mask_secret(value) if is_secret else None,
            value=None if is_secret else value,
            locked_by_env=True,
            env_var=env_var,
        )
    return SettingsResponse(settings=settings)


@router.patch("", response_model=UpdateSettingsResponse)
async def update_settings(
    request: Request,
    body: UpdateSettingsRequest,
) -> UpdateSettingsResponse:
    """Update system settings.

    Validates API keys before saving. Only non-null values are updated.

    This endpoint works without authentication during setup mode (no users exist).
    Otherwise, global admin access is required.
    """
    await require_settings_owner(request)

    service = get_settings_service()
    # Before anything is saved, so a refused update leaves every setting as it was.
    unchanged = _unchanged_deployment_settings(
        {
            key: getattr(body, key)
            for key in RUNTIME_SETTING_ENV_VARS
            if getattr(body, key, None) is not None
        }
    )
    if unchanged:
        body = body.model_copy(update=dict.fromkeys(unchanged))
    await _reject_unservable_bedrock_dimensions(body)
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

    # Every key is saved before any is applied, so the runtimes rebuild once
    # around the complete change, here and on every other replica.
    await _apply_runtime_settings(updated)

    return UpdateSettingsResponse(updated=updated, validation=validation)


@router.delete("/{key}", response_model=DeleteSettingResponse)
async def delete_setting(
    request: Request,
    key: str,
) -> DeleteSettingResponse:
    """Delete a setting from the database.

    After deletion, the setting will fall back to environment variable
    if one is configured.

    Requires global admin access (not available during setup mode).
    """
    if await is_setup_mode():
        raise HTTPException(status_code=403, detail="Cannot delete settings during setup mode")

    await require_settings_owner(request)

    service = get_settings_service()
    deleted = await service.delete(key)

    if deleted:
        # A runtime setting the deployment does not own is cleared from the
        # environment; one it owns keeps the deployment's value.
        await _apply_runtime_settings([key])

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
