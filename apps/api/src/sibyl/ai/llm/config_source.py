"""DB-backed LLM config source for API and worker runtimes."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import cast

from pydantic import SecretStr

from sibyl.services.settings import SettingsService
from sibyl_core.ai.errors import LLMConfigError
from sibyl_core.ai.llm.config import (
    ConfigField,
    EnvConfigSource,
    LLMProviderName,
    LLMSurface,
    ResolvedLLMConfig,
)

_CACHE_TTL_SECONDS = 60
_PROVIDERS: frozenset[str] = frozenset({"anthropic", "gemini", "openai"})

_PROVIDER_KEY_SETTINGS: dict[LLMProviderName, tuple[str, tuple[str, ...]]] = {
    "anthropic": ("anthropic_api_key", ("SIBYL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")),
    "gemini": ("gemini_api_key", ("SIBYL_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")),
    "openai": ("openai_api_key", ("SIBYL_OPENAI_API_KEY", "OPENAI_API_KEY")),
}


class DBSettingsConfigSource:
    def __init__(
        self,
        settings_service: SettingsService,
        *,
        environ: Mapping[str, str] | None = None,
        cache_ttl_seconds: int = _CACHE_TTL_SECONDS,
    ) -> None:
        self._settings = settings_service
        self._environ = environ if environ is not None else os.environ
        self._env = EnvConfigSource(self._environ)
        self._cache_ttl = timedelta(seconds=cache_ttl_seconds)
        self._cache: dict[LLMSurface, ResolvedLLMConfig] = {}

    async def resolve(self, surface: LLMSurface) -> ResolvedLLMConfig:
        cached = self._cache.get(surface)
        if cached and cached.cached_at and datetime.now(UTC) - cached.cached_at < self._cache_ttl:
            return cached

        env_config = await self._env.resolve(surface)
        provider = await self._resolve_provider(surface, env_config.provider)
        resolved = ResolvedLLMConfig(
            surface=surface,
            provider=provider,
            model=await self._resolve_string(surface, "model", env_config.model),
            temperature=await self._resolve_float(
                surface,
                "temperature",
                env_config.temperature,
            ),
            max_tokens=await self._resolve_int(surface, "max_tokens", env_config.max_tokens),
            timeout_seconds=await self._resolve_float(
                surface,
                "timeout_seconds",
                env_config.timeout_seconds,
            ),
            api_key=await self._resolve_api_key(provider.value),
            transport_max_retries=env_config.transport_max_retries,
            cached_at=datetime.now(UTC),
        ).with_model_defaults()
        self._cache[surface] = resolved
        return resolved

    async def invalidate(self, surface: LLMSurface | None = None) -> None:
        if surface is None:
            self._cache.clear()
            return
        self._cache.pop(surface, None)

    async def _resolve_provider(
        self,
        surface: LLMSurface,
        env_field: ConfigField[LLMProviderName],
    ) -> ConfigField[LLMProviderName]:
        if env_field.source == "env":
            return env_field

        raw_value = await self._settings.get_llm_setting(surface.value, "provider")
        if raw_value is None:
            return env_field
        value = raw_value.strip()
        if value not in _PROVIDERS:
            raise LLMConfigError(
                f"Unsupported LLM provider: {value}",
                provider=value,
                surface=surface.value,
            )
        return ConfigField(value=cast("LLMProviderName", value), source="db")

    async def _resolve_string(
        self,
        surface: LLMSurface,
        field: str,
        env_field: ConfigField[str],
    ) -> ConfigField[str]:
        if env_field.source == "env":
            return env_field

        value = await self._settings.get_llm_setting(surface.value, field)
        if value is None:
            return env_field
        return ConfigField(value=value, source="db")

    async def _resolve_float(
        self,
        surface: LLMSurface,
        field: str,
        env_field: ConfigField[float],
    ) -> ConfigField[float]:
        if env_field.source == "env":
            return env_field

        raw_value = await self._settings.get_llm_setting(surface.value, field)
        if raw_value is None:
            return env_field
        try:
            return ConfigField(value=float(raw_value), source="db")
        except ValueError as exc:
            raise LLMConfigError(
                f"Invalid float for llm.{surface.value}.{field}: {raw_value}",
                surface=surface.value,
            ) from exc

    async def _resolve_int(
        self,
        surface: LLMSurface,
        field: str,
        env_field: ConfigField[int | None],
    ) -> ConfigField[int | None]:
        if env_field.source == "env":
            return env_field

        raw_value = await self._settings.get_llm_setting(surface.value, field)
        if raw_value is None:
            return env_field
        try:
            return ConfigField(value=int(raw_value), source="db")
        except ValueError as exc:
            raise LLMConfigError(
                f"Invalid integer for llm.{surface.value}.{field}: {raw_value}",
                surface=surface.value,
            ) from exc

    async def _resolve_api_key(
        self,
        provider: LLMProviderName,
    ) -> ConfigField[SecretStr | None]:
        return await resolve_provider_api_key(self._settings, provider, environ=self._environ)


async def resolve_provider_api_key(
    settings_service: SettingsService,
    provider: LLMProviderName,
    *,
    environ: Mapping[str, str] | None = None,
) -> ConfigField[SecretStr | None]:
    setting_key, env_vars = _PROVIDER_KEY_SETTINGS[provider]
    env_var, env_value = _lookup_env(os.environ if environ is None else environ, env_vars)
    if env_value is not None:
        return ConfigField(
            value=SecretStr(env_value),
            source="env",
            locked_by_env=True,
            env_var=env_var,
        )

    db_value = await settings_service.get_database_value(setting_key)
    if db_value is None:
        return ConfigField(value=None, source="default")
    return ConfigField(value=SecretStr(db_value), source="db")


def _lookup_env(
    environ: Mapping[str, str],
    names: tuple[str, ...],
) -> tuple[str | None, str | None]:
    for name in names:
        value = environ.get(name, "").strip()
        if value:
            return name, value
    return None, None
