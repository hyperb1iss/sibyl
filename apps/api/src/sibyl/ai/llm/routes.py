"""LLM settings API routes."""

from __future__ import annotations

from typing import Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from sibyl.ai.llm.budget import (
    DEFAULT_MONTHLY_ORG_TOKENS,
    DEFAULT_MONTHLY_USER_TOKENS,
    ORG_BUDGET_SETTING,
    USER_BUDGET_SETTING,
)
from sibyl.ai.llm.config_source import resolve_provider_api_key
from sibyl.ai.llm.service import invalidate_llm_runtime
from sibyl.crypto import mask_secret
from sibyl.persistence.operations_runtime import require_settings_owner
from sibyl.services.settings import get_settings_service
from sibyl_core.ai.llm.config import (
    ConfigField,
    LLMProviderName,
    LLMSurface,
    ResolvedLLMConfig,
    get_config_source,
)
from sibyl_core.ai.registry import ModelEntry, ModelKind, model_registry
from sibyl_core.ai.validation import (
    KeyValidationResult,
    ModelValidationResult,
    SurfaceTestResult,
    check_model_availability,
    check_provider_key,
    test_surface_config,
)

router = APIRouter(prefix="/settings/ai", tags=["ai-settings"])

ConfigSourceName = Literal["env", "db", "default"]
_UPDATABLE_FIELDS = frozenset(
    {"provider", "model", "temperature", "max_tokens", "timeout_seconds"}
)


class ConfigValueField(BaseModel):
    value: str | int | float | None
    source: ConfigSourceName
    locked_by_env: bool = False
    env_var: str | None = None


class SecretConfigField(BaseModel):
    configured: bool
    source: ConfigSourceName
    locked_by_env: bool = False
    env_var: str | None = None
    masked: str | None = None


class LLMSurfaceSettings(BaseModel):
    surface: LLMSurface
    provider: ConfigValueField
    model: ConfigValueField
    temperature: ConfigValueField
    max_tokens: ConfigValueField
    timeout_seconds: ConfigValueField
    api_key: SecretConfigField
    cached_at: str | None = None


class BudgetValueField(BaseModel):
    value: int
    source: ConfigSourceName


class LLMBudgetSettings(BaseModel):
    monthly_user_tokens: BudgetValueField
    monthly_org_tokens: BudgetValueField


class LLMSettingsResponse(BaseModel):
    scope: Literal["instance_wide"] = "instance_wide"
    surfaces: dict[LLMSurface, LLMSurfaceSettings]
    budgets: LLMBudgetSettings


class UpdateLLMSurfaceRequest(BaseModel):
    provider: LLMProviderName | None = None
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    timeout_seconds: float | None = Field(default=None, gt=0.0)


class UpdateLLMSurfaceResponse(BaseModel):
    scope: Literal["instance_wide"] = "instance_wide"
    surface: LLMSurfaceSettings
    warning: str | None = None


class UpdateLLMBudgetRequest(BaseModel):
    monthly_user_tokens: int | None = Field(default=None, gt=0)
    monthly_org_tokens: int | None = Field(default=None, gt=0)


class UpdateLLMBudgetResponse(BaseModel):
    scope: Literal["instance_wide"] = "instance_wide"
    budgets: LLMBudgetSettings


class RegistryResponse(BaseModel):
    entries: list[ModelEntry]


@router.get("/llm", response_model=LLMSettingsResponse)
async def get_llm_settings(request: Request) -> LLMSettingsResponse:
    await require_settings_owner(request)

    source = get_config_source()
    service = get_settings_service()
    surfaces = {
        surface: _surface_settings(await source.resolve(surface))
        for surface in LLMSurface
    }
    return LLMSettingsResponse(
        surfaces=surfaces,
        budgets=await _budget_settings(service),
    )


@router.put("/llm/{surface}", response_model=UpdateLLMSurfaceResponse)
async def update_llm_surface(
    request: Request,
    surface: LLMSurface,
    body: UpdateLLMSurfaceRequest,
) -> UpdateLLMSurfaceResponse:
    await require_settings_owner(request)

    fields = _requested_update_fields(body)
    if not fields:
        resolved = await get_config_source().resolve(surface)
        return UpdateLLMSurfaceResponse(surface=_surface_settings(resolved))

    resolved = await get_config_source().resolve(surface)
    _reject_env_locked_updates(resolved, fields)
    warning = _validate_model_selection(resolved, body)

    service = get_settings_service()
    for field in fields:
        await service.set_llm_setting(surface.value, field, getattr(body, field))

    await invalidate_llm_runtime(surface)
    updated = await get_config_source().resolve(surface)
    return UpdateLLMSurfaceResponse(surface=_surface_settings(updated), warning=warning)


@router.put("/llm-budget", response_model=UpdateLLMBudgetResponse)
async def update_llm_budget(
    request: Request,
    body: UpdateLLMBudgetRequest,
) -> UpdateLLMBudgetResponse:
    await require_settings_owner(request)

    service = get_settings_service()
    field_keys = {
        "monthly_user_tokens": USER_BUDGET_SETTING,
        "monthly_org_tokens": ORG_BUDGET_SETTING,
    }
    for field in body.model_fields_set:
        key = field_keys[field]
        value = getattr(body, field)
        if value is None:
            await service.delete(key)
        else:
            await service.set(
                key,
                str(value),
                is_secret=False,
                description=f"LLM budget {field}",
            )
    return UpdateLLMBudgetResponse(budgets=await _budget_settings(service))


@router.post("/llm/{surface}/test", response_model=SurfaceTestResult)
async def test_llm_surface(request: Request, surface: LLMSurface) -> SurfaceTestResult:
    await require_settings_owner(request)
    return await test_surface_config(surface, get_config_source())


@router.post("/keys/{provider}/test", response_model=KeyValidationResult)
async def test_provider_key(
    request: Request,
    provider: LLMProviderName,
) -> KeyValidationResult:
    await require_settings_owner(request)
    key = await resolve_provider_api_key(get_settings_service(), provider)
    if key.value is None:
        raise HTTPException(status_code=400, detail=f"No {provider} API key configured")
    return await check_provider_key(provider, key.value.get_secret_value())


@router.post("/models/{model_alias}/test", response_model=ModelValidationResult)
async def test_model_availability(
    request: Request,
    model_alias: str,
) -> ModelValidationResult:
    await require_settings_owner(request)
    entry = model_registry.get(model_alias)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown model: {model_alias}")
    if entry.kind is not ModelKind.LLM:
        raise HTTPException(status_code=400, detail=f"Model is not an LLM: {model_alias}")

    raw_provider = entry.provider
    if raw_provider not in {"anthropic", "gemini", "openai"}:
        raise HTTPException(status_code=400, detail=f"Provider is not supported: {raw_provider}")
    provider = cast("LLMProviderName", raw_provider)

    key = await resolve_provider_api_key(get_settings_service(), provider)
    if key.value is None:
        raise HTTPException(status_code=400, detail=f"No {provider} API key configured")
    return await check_model_availability(
        provider,
        entry.provider_model_id,
        key.value.get_secret_value(),
    )


@router.get("/registry", response_model=RegistryResponse)
async def get_ai_registry(
    request: Request,
    kind: ModelKind | None = Query(default=None),
) -> RegistryResponse:
    await require_settings_owner(request)
    return RegistryResponse(entries=model_registry.entries(kind=kind))


def _surface_settings(resolved: ResolvedLLMConfig) -> LLMSurfaceSettings:
    return LLMSurfaceSettings(
        surface=resolved.surface,
        provider=_value_field(resolved.provider),
        model=_value_field(resolved.model),
        temperature=_value_field(resolved.temperature),
        max_tokens=_value_field(resolved.max_tokens),
        timeout_seconds=_value_field(resolved.timeout_seconds),
        api_key=_secret_field(resolved.api_key),
        cached_at=resolved.cached_at.isoformat() if resolved.cached_at else None,
    )


def _value_field(field: ConfigField[Any]) -> ConfigValueField:
    return ConfigValueField(
        value=field.value,
        source=field.source,
        locked_by_env=field.locked_by_env,
        env_var=field.env_var,
    )


def _secret_field(field: ConfigField[Any]) -> SecretConfigField:
    raw_value = field.value.get_secret_value() if field.value is not None else None
    return SecretConfigField(
        configured=bool(raw_value),
        source=field.source,
        locked_by_env=field.locked_by_env,
        env_var=field.env_var,
        masked=mask_secret(raw_value) if raw_value else None,
    )


async def _budget_settings(service) -> LLMBudgetSettings:
    return LLMBudgetSettings(
        monthly_user_tokens=await _budget_value(
            service,
            USER_BUDGET_SETTING,
            default=DEFAULT_MONTHLY_USER_TOKENS,
        ),
        monthly_org_tokens=await _budget_value(
            service,
            ORG_BUDGET_SETTING,
            default=DEFAULT_MONTHLY_ORG_TOKENS,
        ),
    )


async def _budget_value(
    service,
    key: str,
    *,
    default: int,
) -> BudgetValueField:
    raw_value, source = await service.get_with_source(key)
    if raw_value is None:
        return BudgetValueField(value=default, source="default")
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid LLM budget setting: {key}") from exc
    if value <= 0:
        raise HTTPException(status_code=500, detail=f"Invalid LLM budget setting: {key}")
    return BudgetValueField(
        value=value,
        source="db" if source == "database" else "env",
    )


def _requested_update_fields(body: UpdateLLMSurfaceRequest) -> list[str]:
    fields = [field for field in body.model_fields_set if field in _UPDATABLE_FIELDS]
    for field in ("provider", "model"):
        if field in fields and getattr(body, field) is None:
            raise HTTPException(status_code=422, detail=f"{field} cannot be cleared")
    return fields


def _reject_env_locked_updates(resolved: ResolvedLLMConfig, fields: list[str]) -> None:
    locked = [
        {
            "field": field,
            "env_var": getattr(resolved, field).env_var,
        }
        for field in fields
        if getattr(resolved, field).locked_by_env
    ]
    if locked:
        raise HTTPException(
            status_code=409,
            detail={"code": "LOCKED_BY_ENV", "fields": locked},
        )


def _validate_model_selection(
    resolved: ResolvedLLMConfig,
    body: UpdateLLMSurfaceRequest,
) -> str | None:
    provider = body.provider if "provider" in body.model_fields_set else resolved.provider.value
    model = body.model if "model" in body.model_fields_set else resolved.model.value
    if provider is None or model is None:
        return None

    entry = model_registry.get(model, kind=ModelKind.LLM)
    if entry is None:
        return "unverified_model"
    if entry.provider != provider:
        raise HTTPException(
            status_code=422,
            detail=f"Model {model} belongs to provider {entry.provider}, not {provider}",
        )
    return None
